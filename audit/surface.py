#!/usr/bin/env python3
"""Lists the surface an audit has to cover, starting from a seed, in any project.

The surface is the input of every audit pass: the routes in scope, the files
that serve them, where they write to storage, every caller of those routes
anywhere under the root (a front end in a sibling folder included), the files
that use those callers (the screens), and the recent commits on those files.

A form the reader does not recognise goes to `unknown` with the file and the
reason. An empty list means "none found", never "not understood".

Exit codes: 0 surface found, 2 invalid arguments, 3 the seed matched nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

SKIP_DIRS = frozenset({
    'node_modules', 'target', 'build', 'dist', 'out', 'coverage', 'vendor',
    'bin', 'obj', '__pycache__', 'venv', '.venv',
})
SOURCE_EXT = frozenset({
    '.java', '.kt', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.vue', '.html',
    '.py', '.go', '.rb', '.cs', '.rs', '.php', '.dart', '.swift', '.scala',
})
JVM_EXT = frozenset({'.java', '.kt'})
JS_EXT = frozenset({'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'})
CODE_EXT = JVM_EXT | JS_EXT
MAX_FILE_BYTES = 1_000_000
LINE_TEXT_MAX = 200
GIT_TIMEOUT_S = 60
DEFAULT_DEPTH = 2
DEFAULT_COMMITS = 20
NO_REPO = '\0none'


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    file: str
    line: int
    framework: str


@dataclass(frozen=True)
class Unknown:
    file: str
    line: int
    reason: str


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    kind: str
    text: str


@dataclass(frozen=True)
class Caller:
    route: str
    file: str
    line: int
    match: str
    test: bool
    text: str


@dataclass(frozen=True)
class Seed:
    route_prefix: str | None = None
    paths: tuple[str, ...] = ()
    diff_base: str | None = None


class SurfaceError(Exception):
    """Invalid input: the caller passed a seed or root that cannot be read."""


class Tree:
    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        self.files: list[str] = []
        self.repos: list[str] = []
        self.skipped_large: list[str] = []
        self.notes: list[str] = []
        self._text: dict[str, str | None] = {}
        self._load()

    def _load(self) -> None:
        """Every source file of each repository under the root, as git lists it.

        Inside a repository, git's own list (tracked plus untracked that are
        not ignored) decides what is source, so build output and scratch files
        the project ignores stay out. Files outside every repository are read
        only when the root holds no repository at all.
        """
        walked: list[str] = []
        unreadable: list[str] = []
        for current, dirs, names in os.walk(self.root, onerror=lambda error: unreadable.append(str(error.filename))):
            if os.path.exists(os.path.join(current, '.git')):
                self.repos.append(self.rel(current))
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS)
            walked += [self.rel(os.path.join(current, name)) for name in names
                       if os.path.splitext(name)[1] in SOURCE_EXT]
        if unreadable:
            self.notes.append(f'{len(unreadable)} folders could not be listed and were not read: '
                              + ', '.join(self.rel(path) for path in unreadable[:20]))
        if not self.repos:
            candidates = walked
        else:
            candidates = []
            for repo in self.repos:
                listed, error = _git(self, repo, ['ls-files', '--cached', '--others', '--exclude-standard', '-z'])
                if listed is None:
                    self.notes.append(f'git ls-files failed in {repo or "."} ({error}); its files were read '
                                      'from disk, ignored files included')
                    candidates += [rel for rel in walked if self.repo_of(rel) == repo]
                    continue
                for name in listed.split('\0'):
                    if name and not name.endswith('/') and os.path.splitext(name)[1] in SOURCE_EXT:
                        candidates.append((repo + '/' + name) if repo else name)
            outside = [rel for rel in walked if self.repo_of(rel) == NO_REPO]
            if outside:
                self.notes.append(f'{len(outside)} source files outside every git repository under the root '
                                  'were not read')
        for rel in sorted(set(candidates)):
            full = self.abs(rel)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = MAX_FILE_BYTES + 1
            if size > MAX_FILE_BYTES:
                self.skipped_large.append(rel)
                continue
            self.files.append(rel)

    def rel(self, path: str) -> str:
        rel = os.path.relpath(path, self.root)
        return '' if rel == '.' else rel.replace(os.sep, '/')

    def abs(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def text(self, rel: str) -> str | None:
        if rel not in self._text:
            try:
                with open(self.abs(rel), 'rb') as handle:
                    raw = handle.read()
            except OSError:
                self._text[rel] = None
                self.skipped_large.append(rel)
                return None
            self._text[rel] = None if b'\0' in raw[:4096] else raw.decode('utf-8', errors='replace')
        return self._text[rel]

    def repo_of(self, rel: str) -> str:
        owners = [repo for repo in self.repos if repo == '' or rel == repo or rel.startswith(repo + '/')]
        return max(owners, key=len) if owners else NO_REPO


def _line(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


def _line_text(text: str, line: int) -> str:
    lines = text.splitlines()
    return lines[line - 1].strip()[:LINE_TEXT_MAX] if 0 < line <= len(lines) else ''


def _closing(text: str, open_idx: int) -> int | None:
    depth = 0
    quote = ''
    i = open_idx
    while i < len(text):
        char = text[i]
        if quote:
            if char == '\\':
                i += 2
                continue
            if char == quote:
                quote = ''
        elif char in '"\'`':
            quote = char
        elif char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _args_at(text: str, pos: int) -> tuple[str | None, int]:
    """Arguments of the call or annotation whose name ends at `pos`.

    Returns ('', pos) when there are no parentheses and (None, pos) when they
    never close.
    """
    j = pos
    while j < len(text) and text[j] in ' \t':
        j += 1
    if j >= len(text) or text[j] != '(':
        return '', pos
    close = _closing(text, j)
    if close is None:
        return None, pos
    return text[j + 1:close], close


def _split_top(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ''
    start = 0
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == '\\':
                i += 2
                continue
            if char == quote:
                quote = ''
        elif char in '"\'`':
            quote = char
        elif char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [part for part in parts if part.strip()]


_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`([^`$]*)`', re.DOTALL)


def _literal(expr: str) -> str | None:
    match = _STRING.fullmatch(expr.strip())
    if not match:
        return None
    return next(group for group in match.groups() if group is not None)


def _literals(expr: str) -> list[str] | None:
    expr = expr.strip()
    if expr[:1] in '{[' and expr[-1:] in '}]':
        items = _split_top(expr[1:-1])
        values = [_literal(item) for item in items]
        if any(value is None for value in values):
            return None
        return [value for value in values if value is not None] or ['']
    value = _literal(expr)
    return None if value is None else [value]


def _named(args: str, names: tuple[str, ...]) -> str | None:
    for part in _split_top(args):
        match = re.match(r'\s*(\w+)\s*[=:]\s*(.*)$', part, re.DOTALL)
        if match and match.group(1) in names:
            return match.group(2)
    return None


def normalize(path: str) -> str:
    segments: list[str] = []
    for segment in path.split('/'):
        segment = segment.strip()
        if not segment:
            continue
        spring = re.fullmatch(r'\{(\w+)(?::.*)?\}', segment)
        colon = re.fullmatch(r':(\w+)(?:\(.*\))?\??', segment)
        if spring:
            segment = '{' + spring.group(1) + '}'
        elif colon:
            segment = '{' + colon.group(1) + '}'
        segments.append(segment)
    return '/' + '/'.join(segments)


def _join(prefix: str, path: str) -> str:
    return normalize(prefix + '/' + path)


_SPRING_CONTROLLER = re.compile(r'@(?:Rest)?Controller\b')
_SPRING_MAPPING = re.compile(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\b')
_DECL = re.compile(r'\b(?:class|interface|enum|record|object)\s+([A-Z]\w*)')


def _spring_paths(args: str) -> list[str] | None:
    if not args.strip():
        return ['']
    named = _named(args, ('value', 'path'))
    if named is not None:
        return _literals(named)
    first = _split_top(args)[0]
    if re.match(r'\s*\w+\s*=', first):
        return ['']
    return _literals(first)


def _read_spring(rel: str, text: str, routes: list[Route], unknown: list[Unknown]) -> None:
    is_controller = bool(_SPRING_CONTROLLER.search(text))
    mappings = list(_SPRING_MAPPING.finditer(text))
    if not mappings and not is_controller:
        return
    declaration = _DECL.search(text)
    type_pos = declaration.start() if declaration else 0
    prefixes = ['']
    found = False
    for match in mappings:
        args, _ = _args_at(text, match.end())
        line = _line(text, match.start())
        kind = match.group(1)
        if args is None:
            unknown.append(Unknown(rel, line, f'@{kind}Mapping arguments never close'))
            continue
        paths = _spring_paths(args)
        if match.start() < type_pos:
            if paths is None:
                unknown.append(Unknown(rel, line, 'class-level @RequestMapping path is not a literal; '
                                                  'the routes of this type were not resolved'))
                return
            prefixes = paths
            continue
        found = True
        if paths is None:
            unknown.append(Unknown(rel, line, f'@{kind}Mapping path is not a literal'))
            continue
        methods = [kind.upper()] if kind != 'Request' else (re.findall(r'RequestMethod\.(\w+)', args) or ['ANY'])
        for prefix in prefixes:
            for path in paths:
                for method in methods:
                    routes.append(Route(method, _join(prefix, path), rel, line, 'spring'))
    if is_controller and not found:
        unknown.append(Unknown(rel, _line(text, type_pos), 'controller with no route mapping recognised'))


_NEST_CONTROLLER = re.compile(r'@Controller\b')
_NEST_METHOD = re.compile(r'@(Get|Post|Put|Delete|Patch|All|Options|Head)\b')


def _nest_prefix(args: str) -> list[str] | None:
    if not args.strip():
        return ['']
    first = _split_top(args)[0].strip()
    if first.startswith('{'):
        path = _named(first[1:-1], ('path',))
        return [''] if path is None else _literals(path)
    return _literals(first)


def _read_nest(rel: str, text: str, routes: list[Route], unknown: list[Unknown]) -> None:
    controllers: list[tuple[int, list[str] | None]] = []
    for match in _NEST_CONTROLLER.finditer(text):
        args, _ = _args_at(text, match.end())
        prefix = None if args is None else _nest_prefix(args)
        if prefix is None:
            unknown.append(Unknown(rel, _line(text, match.start()),
                                   '@Controller path is not a literal; the routes of this controller were not resolved'))
        controllers.append((match.start(), prefix))
    found = False
    for match in _NEST_METHOD.finditer(text):
        owner = [prefix for pos, prefix in controllers if pos < match.start()]
        if not owner:
            continue
        prefixes = owner[-1]
        if prefixes is None:
            continue
        found = True
        line = _line(text, match.start())
        args, _ = _args_at(text, match.end())
        paths = None if args is None else ([''] if not args.strip() else _literals(_split_top(args)[0]))
        if paths is None:
            unknown.append(Unknown(rel, line, f'@{match.group(1)} path is not a literal'))
            continue
        for prefix in prefixes:
            for path in paths:
                routes.append(Route(match.group(1).upper(), _join(prefix, path), rel, line, 'nestjs'))
    if controllers and not found and all(prefix is not None for _, prefix in controllers):
        unknown.append(Unknown(rel, _line(text, controllers[0][0]), 'controller with no route decorator recognised'))


_EXPRESS_HINT = re.compile(
    r'''require\(\s*['"](?:express|fastify|koa-router|@koa/router)['"]'''
    r'''|from\s+['"](?:express|fastify|koa-router|@koa/router|hono)['"]|\bRouter\(\)''')
_HTTP_VERBS = 'get|post|put|delete|patch|all|options|head'
_EXPRESS_CALL = re.compile(r'\b[A-Za-z_$][\w$]*\.(' + _HTTP_VERBS + r')\(')
_EXPRESS_ROUTE = re.compile(r'\.route\(')
_CHAINED_VERB = re.compile(r'\s*\.(' + _HTTP_VERBS + r')\(')


def _read_express(rel: str, text: str, routes: list[Route], unknown: list[Unknown]) -> None:
    for match in _EXPRESS_CALL.finditer(text):
        args, _ = _args_at(text, match.end() - 1)
        if args is None:
            continue
        parts = _split_top(args)
        if len(parts) < 2:
            continue
        first = parts[0].strip()
        line = _line(text, match.start())
        path = _literal(first)
        if path is not None:
            routes.append(Route(match.group(1).upper(), normalize(path), rel, line, 'express'))
        elif first.startswith('`'):
            unknown.append(Unknown(rel, line, 'route path is a template literal'))
        elif re.fullmatch(r'/.+/[a-z]*', first):
            unknown.append(Unknown(rel, line, 'route path is a regular expression'))
        elif re.fullmatch(r'[\w$.\[\]\'"]+', first):
            unknown.append(Unknown(rel, line, 'route path is not a literal'))
    for match in _EXPRESS_ROUTE.finditer(text):
        args, close = _args_at(text, match.end() - 1)
        if args is None:
            continue
        line = _line(text, match.start())
        path = _literal(args)
        if path is None:
            unknown.append(Unknown(rel, line, '.route() path is not a literal'))
            continue
        methods: list[str] = []
        cursor = close + 1
        while True:
            chained = _CHAINED_VERB.match(text, cursor)
            if not chained:
                break
            methods.append(chained.group(1).upper())
            _, cursor = _args_at(text, chained.end() - 1)
            cursor += 1
        for method in methods or ['ANY']:
            routes.append(Route(method, normalize(path), rel, line, 'express'))


_FOREIGN: tuple[tuple[str, str, re.Pattern[str], str | None], ...] = (
    ('.go', 'go', re.compile(r'\.(?:HandleFunc|Handle|GET|POST|PUT|DELETE|PATCH|Get|Post|Put|Delete|Patch)\(\s*"/'), None),
    ('.rs', 'rust', re.compile(r'#\[(?:get|post|put|delete|patch)\(|\.route\(\s*"/'), None),
    ('.cs', 'csharp', re.compile(r'\[(?:Http(?:Get|Post|Put|Delete|Patch)|Route)\b|\.Map(?:Get|Post|Put|Delete|Patch)\('), None),
    ('.py', 'python', re.compile(r'@\w+(?:\.\w+)*\.(?:route|get|post|put|delete|patch)\('), None),
    ('.py', 'python', re.compile(r'\b(?:re_)?path\(\s*r?[\'"]'), 'urls.py'),
    ('.rb', 'ruby', re.compile(r'^\s*(?:get|post|put|patch|delete|resources?|namespace)\s+[\'":]', re.MULTILINE), 'routes.rb'),
    ('.php', 'php', re.compile(r'Route::(?:get|post|put|patch|delete|any|match)\(|#\[Route\('), None),
)


def _read_foreign(rel: str, text: str, unknown: list[Unknown]) -> None:
    ext = os.path.splitext(rel)[1]
    name = os.path.basename(rel)
    for foreign_ext, stack, pattern, only_name in _FOREIGN:
        if ext != foreign_ext or (only_name and name != only_name):
            continue
        match = pattern.search(text)
        if match:
            unknown.append(Unknown(rel, _line(text, match.start()),
                                   f'route declarations in a stack the surface reader does not parse ({stack})'))
            return


def read_routes(tree: Tree) -> tuple[list[Route], list[Unknown]]:
    routes: list[Route] = []
    unknown: list[Unknown] = []
    for rel in tree.files:
        text = tree.text(rel)
        if text is None:
            continue
        ext = os.path.splitext(rel)[1]
        if ext in JVM_EXT:
            _read_spring(rel, text, routes, unknown)
        elif ext in JS_EXT:
            if _NEST_CONTROLLER.search(text):
                _read_nest(rel, text, routes, unknown)
            elif _EXPRESS_HINT.search(text):
                _read_express(rel, text, routes, unknown)
        else:
            _read_foreign(rel, text, unknown)
    return routes, unknown


_KOTLIN_PARENTS = re.compile(r'\bclass\s+[A-Z]\w*(?:\s*\([^)]*\))?\s*:\s*([^{;\n]+)')
_IMPLEMENTS = re.compile(r'\bclass\s+[A-Z]\w*[^{;]*?\bimplements\s+([^{;]+)')
_TYPE_NAME = re.compile(r'\b[A-Z]\w+\b')


def _interfaces(text: str) -> list[str]:
    """Interfaces the types in `text` implement; base classes are left out.

    Following a shared base class would link every subclass to every other
    and pull the whole codebase into scope. A Kotlin supertype written with a
    constructor call (`: Base()`) is a class and is skipped for that reason.
    """
    names = [name for group in _IMPLEMENTS.findall(text) for name in _TYPE_NAME.findall(group)]
    for group in _KOTLIN_PARENTS.findall(text):
        for part in _split_top(group):
            first = _TYPE_NAME.search(part)
            if first and '(' not in part:
                names.append(first.group(0))
    return names


class TypeIndex:
    def __init__(self, tree: Tree) -> None:
        self.tree = tree
        self.declared: dict[str, set[str]] = {}
        self.implemented: dict[str, set[str]] = {}
        for rel in tree.files:
            if os.path.splitext(rel)[1] not in CODE_EXT:
                continue
            text = tree.text(rel)
            if text is None:
                continue
            for name in _DECL.findall(text):
                self.declared.setdefault(name, set()).add(rel)
            for name in _interfaces(text):
                self.implemented.setdefault(name, set()).add(rel)

    def files_for(self, name: str, repo: str) -> set[str]:
        found = self.declared.get(name, set()) | self.implemented.get(name, set())
        return {rel for rel in found if self.tree.repo_of(rel) == repo}

    def down(self, start: set[str], depth: int) -> set[str]:
        seen = set(start)
        frontier = set(start)
        for _ in range(depth):
            following: set[str] = set()
            for rel in frontier:
                text = self.tree.text(rel)
                if text is None or os.path.splitext(rel)[1] not in CODE_EXT:
                    continue
                repo = self.tree.repo_of(rel)
                for name in set(_TYPE_NAME.findall(text)) & self.declared.keys():
                    following |= self.files_for(name, repo) - seen
            seen |= following
            frontier = following
        return seen

    def up(self, start: set[str], depth: int) -> set[str]:
        seen = set(start)
        frontier = set(start)
        for _ in range(depth):
            names: dict[str, set[str]] = {}
            for rel in frontier:
                text = self.tree.text(rel) or ''
                repo = self.tree.repo_of(rel)
                implemented = [name for name in _interfaces(text)
                               if any(self.tree.repo_of(f) == repo for f in self.declared.get(name, ()))]
                for name in _DECL.findall(text) + implemented:
                    names.setdefault(repo, set()).add(name)
            following: set[str] = set()
            for repo, repo_names in names.items():
                pattern = re.compile(r'\b(?:' + '|'.join(sorted(map(re.escape, repo_names))) + r')\b')
                for rel in self.tree.files:
                    if rel in seen or os.path.splitext(rel)[1] not in CODE_EXT or self.tree.repo_of(rel) != repo:
                        continue
                    if pattern.search(self.tree.text(rel) or ''):
                        following.add(rel)
            seen |= following
            frontier = following
        return seen


_COMMENT_LINE = re.compile(r'\s*(?://|/?\*)')
_SQL_WRITE = re.compile(r'\b(?:INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b', re.IGNORECASE)
_SITES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    'jvm': (
        ('write', re.compile(r'\.(?:save|saveAll|saveAndFlush|saveAllAndFlush|delete|deleteById|deleteAll|'
                             r'deleteAllById|deleteAllInBatch|persist|merge|remove)\(')),
        ('write', re.compile(r'@Modifying\b')),
        ('write', re.compile(r'\b\w*[jJ]dbc\w*\.(?:update|batchUpdate)\(')),
        ('write', _SQL_WRITE),
        ('transaction', re.compile(r'@Transactional\b|TransactionTemplate\b')),
        ('lock', re.compile(r'@Lock\(|@Version\b|\bFOR\s+UPDATE\b|PESSIMISTIC_\w+|OPTIMISTIC\w*', re.IGNORECASE)),
    ),
    'js': (
        ('write', re.compile(r'\.(?:create|createMany|update|updateMany|upsert|delete|deleteMany|save|insert|'
                             r'remove|softDelete|increment|decrement)\(')),
        ('write', _SQL_WRITE),
        ('transaction', re.compile(r'\$transaction\(|\.transaction\(|@Transaction\b|startTransaction\(')),
        ('lock', re.compile(r'\bFOR\s+UPDATE\b|@VersionColumn\b|pessimistic_\w+|optimistic\w*', re.IGNORECASE)),
    ),
}


def read_sites(tree: Tree, files: set[str]) -> list[Site]:
    sites: list[Site] = []
    for rel in sorted(files):
        ext = os.path.splitext(rel)[1]
        family = 'jvm' if ext in JVM_EXT else 'js' if ext in JS_EXT else None
        text = tree.text(rel)
        if family is None or text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _COMMENT_LINE.match(line):
                continue
            for kind, pattern in _SITES[family]:
                if pattern.search(line):
                    sites.append(Site(rel, number, kind, line.strip()[:LINE_TEXT_MAX]))
                    break
    return sites


_PARAM = r'''(?:\$\{[^}]*\}|\{[^}]*\}|:\w+|[^/\s'"`?]+)'''
_TEST_PATH = re.compile(r'(?:\.spec\.|\.test\.|/tests?/|__tests__|Tests?\.(?:java|kt)$|_test\.go$|IT\.java$)')


def _segments(path: str) -> tuple[list[str], list[str]]:
    segments = [segment for segment in path.split('/') if segment]
    return segments, [segment for segment in segments if not segment.startswith('{')]


def read_callers(tree: Tree, routes: list[Route]) -> tuple[list[Caller], list[Unknown]]:
    callers: list[Caller] = []
    unknown: list[Unknown] = []
    declarations = {(route.file, route.line) for route in routes}
    by_path: dict[str, list[Route]] = {}
    for route in routes:
        by_path.setdefault(route.path, []).append(route)
    for path, declared in sorted(by_path.items()):
        segments, static = _segments(path)
        if not static:
            unknown.append(Unknown(declared[0].file, declared[0].line,
                                   f'route {path} has no static segment; its callers cannot be searched'))
            continue
        exact = re.compile('/' + '/'.join(re.escape(s) if not s.startswith('{') else _PARAM for s in segments)
                           + r'''(?=['"`?#]|$)''')
        loose = re.compile('.*?'.join(r'/' + re.escape(s) + r'''(?=[^\w-]|$)''' for s in static)) \
            if len(static) > 1 else None
        last = static[-1]
        first_literal = re.compile(r'''['"`/]''' + re.escape(static[0]) + r'''['"`/?]''')
        last_literal = re.compile(r'''['"`/]''' + re.escape(last) + r'''['"`/?]''')
        for rel in tree.files:
            text = tree.text(rel)
            if text is None or last not in text:
                continue
            matched = False
            for number, line in enumerate(text.splitlines(), start=1):
                if (rel, number) in declarations:
                    continue
                kind = 'exact' if exact.search(line) else \
                    'loose' if loose and re.search('[\'"`]', line) and loose.search(line) else None
                if kind:
                    matched = True
                    callers.append(Caller(path, rel, number, kind, bool(_TEST_PATH.search(rel)),
                                          line.strip()[:LINE_TEXT_MAX]))
            declares = any(route.file == rel for route in declared)
            if not matched and not declares and len(static) > 1 and first_literal.search(text):
                hit = last_literal.search(text)
                if hit:
                    number = _line(text, hit.start())
                    callers.append(Caller(path, rel, number, 'file', bool(_TEST_PATH.search(rel)),
                                          _line_text(text, number)))
    return callers, unknown


def _git(tree: Tree, repo: str, args: list[str]) -> tuple[str | None, str]:
    try:
        done = subprocess.run(['git', '-C', tree.abs(repo), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except FileNotFoundError:
        return None, 'git is not installed'
    except subprocess.TimeoutExpired:
        return None, f'git {args[0]} timed out after {GIT_TIMEOUT_S}s'
    if done.returncode != 0:
        return None, done.stderr.strip()[:LINE_TEXT_MAX] or f'git {args[0]} exited {done.returncode}'
    return done.stdout, ''


def changed_files(tree: Tree, base: str) -> tuple[set[str], list[Unknown]]:
    files: set[str] = set()
    unknown: list[Unknown] = []
    for repo in tree.repos:
        verified, error = _git(tree, repo, ['rev-parse', '--verify', '--quiet', base + '^{commit}'])
        if verified is None:
            unknown.append(Unknown(repo or '.', 0, f'base {base} not found in this repository: {error or "unknown ref"}'))
            continue
        out, error = _git(tree, repo, ['diff', '--name-only', base + '...HEAD'])
        if out is None:
            unknown.append(Unknown(repo or '.', 0, f'git diff failed: {error}'))
            continue
        for name in out.splitlines():
            rel = (repo + '/' + name) if repo else name
            if rel in tree.files:
                files.add(rel)
    return files, unknown


def read_commits(tree: Tree, files: set[str], limit: int) -> tuple[list[dict[str, object]], list[Unknown]]:
    commits: list[dict[str, object]] = []
    unknown: list[Unknown] = []
    by_repo: dict[str, list[str]] = {}
    for rel in files:
        repo = tree.repo_of(rel)
        if repo != NO_REPO:
            by_repo.setdefault(repo, []).append(rel[len(repo) + 1:] if repo else rel)
    for repo, names in sorted(by_repo.items()):
        out, error = _git(tree, repo, ['log', f'-n{limit}', '--date=short', '--format=%x1e%h%x1f%ad%x1f%s',
                                       '--name-only', '--', *sorted(names)])
        if out is None:
            unknown.append(Unknown(repo or '.', 0, f'git log failed: {error}'))
            continue
        for record in out.split('\x1e')[1:]:
            head, _, body = record.partition('\n')
            sha, date, subject = (head.split('\x1f') + ['', ''])[:3]
            touched = [(repo + '/' + n) if repo else n for n in body.split('\n') if n.strip()]
            commits.append({'repo': repo or '.', 'sha': sha, 'date': date, 'subject': subject[:LINE_TEXT_MAX],
                            'files': sorted(touched)})
    return commits, unknown


def _inside(tree: Tree, path: str) -> str:
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(tree.root, path))
    if os.path.commonpath([full, tree.root]) != tree.root:
        raise SurfaceError(f'seed path is outside the root: {path}')
    if not os.path.exists(full):
        raise SurfaceError(f'seed path does not exist: {path}')
    return tree.rel(full)


def build(root: str, seed: Seed, depth: int = DEFAULT_DEPTH, commits: int = DEFAULT_COMMITS) -> dict[str, object]:
    if not os.path.isdir(root):
        raise SurfaceError(f'root is not a directory: {root}')
    tree = Tree(root)
    all_routes, route_unknown = read_routes(tree)
    index = TypeIndex(tree)
    unknown: list[Unknown] = []
    limits = [
        f'files reached from the routes follow type references {depth} levels deep, within the same repository',
        ('mount and global prefixes (Express app.use, Nest setGlobalPrefix, Spring context-path) are not applied: '
         'route paths are as declared, and callers are matched on that path'),
    ]
    if seed.route_prefix is not None:
        prefix = normalize(seed.route_prefix)
        routes = [r for r in all_routes if prefix == '/' or r.path == prefix or r.path.startswith(prefix + '/')]
        module = index.down({r.file for r in routes}, depth)
        unknown += route_unknown
    elif seed.paths:
        roots = [_inside(tree, path) for path in seed.paths]
        seeded = {rel for rel in tree.files if any(root_rel == '' or rel == root_rel or rel.startswith(root_rel + '/')
                                                  for root_rel in roots)}
        routes = [r for r in all_routes if r.file in seeded]
        module = index.down(seeded, depth)
        unknown += [u for u in route_unknown if u.file in module]
    elif seed.diff_base is not None:
        if seed.diff_base.startswith('-'):
            raise SurfaceError(f'invalid base ref: {seed.diff_base}')
        changed, diff_unknown = changed_files(tree, seed.diff_base)
        unknown += diff_unknown
        limits.append('only changed source files are listed; changed configuration, migrations in other '
                      'formats and docs are not read')
        reaching = index.up(changed, depth)
        route_files = {r.file for r in all_routes}
        module = changed | (reaching & route_files)
        routes = [r for r in all_routes if r.file in module]
        unknown += [u for u in route_unknown if u.file in module]
    else:
        raise SurfaceError('a seed is required: route prefix, path or diff base')

    callers, caller_unknown = read_callers(tree, routes)
    caller_files = {c.file for c in callers if not c.test}
    consumers = sorted(rel for rel in index.up(caller_files, 1) - caller_files - module
                       if not _TEST_PATH.search(rel))
    history, git_unknown = read_commits(tree, module, commits)
    unknown += caller_unknown + git_unknown
    limits += tree.notes
    if tree.skipped_large:
        limits.append(f'{len(tree.skipped_large)} files over {MAX_FILE_BYTES} bytes or unreadable were not read: '
                      + ', '.join(tree.skipped_large[:20]) + (' ...' if len(tree.skipped_large) > 20 else ''))
    return {
        'repos': sorted(repo or '.' for repo in tree.repos),
        'routes': [asdict(r) for r in sorted(set(routes), key=lambda r: (r.path, r.method, r.file, r.line))],
        'files': sorted(module),
        'sites': [asdict(s) for s in read_sites(tree, module)],
        'callers': [asdict(c) for c in sorted(callers, key=lambda c: (c.route, c.file, c.line))],
        'consumers': consumers,
        'commits': history,
        'unknown': [asdict(u) for u in sorted(set(unknown), key=lambda u: (u.file, u.line, u.reason))],
        'limits': limits,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='List the surface an audit has to cover.')
    parser.add_argument('--root', default='.', help='folder holding every repository in scope (default: cwd)')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--route-prefix', help='routes whose path starts with this prefix')
    group.add_argument('--path', action='append', help='a folder or file inside the root (repeatable)')
    group.add_argument('--diff', metavar='BASE', help='what changed in BASE...HEAD, in every repository under the root')
    parser.add_argument('--depth', type=int, default=DEFAULT_DEPTH, help='type-reference levels to follow')
    parser.add_argument('--commits', type=int, default=DEFAULT_COMMITS, help='recent commits to list per repository')
    args = parser.parse_args(argv)
    seed = Seed(route_prefix=args.route_prefix, paths=tuple(args.path or ()), diff_base=args.diff)
    try:
        surface = build(args.root, seed, depth=max(0, args.depth), commits=max(1, args.commits))
    except SurfaceError as error:
        print(f'surface: {error}', file=sys.stderr)
        return 2
    print(json.dumps(surface, indent=2, ensure_ascii=False))
    if not surface['routes'] and not surface['files']:
        print('surface: the seed matched no route and no file', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
