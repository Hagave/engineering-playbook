"""
What a Bash command read and whether it validated, from its text.

Most file views in real sessions are `cat` / `sed -n` / `head` inside Bash, not
the Read tool, so "did the model look at this file before editing it" cannot
be answered from Read calls alone. Tokenizing is the playbook's own
(`playbook_core.shell_segments`), so quoting and substitutions are read the
same way the gates read them.
"""
import os
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'hooks'))
import playbook_core  # noqa: E402


@dataclass(frozen=True)
class BashRead:
    path: str
    start_line: int | None
    end_line: int | None
    full: bool


_WHOLE_FILE_VIEWERS = frozenset({'cat', 'nl', 'less', 'more', 'bat'})
_GREPS = frozenset({'grep', 'egrep', 'fgrep', 'rg'})
_GREP_VALUE_OPTS = frozenset({'-e', '-f', '-m', '-A', '-B', '-C', '--include', '--exclude',
                              '-g', '-t', '--max-count', '--regexp', '--file'})
_GREP_PATTERN_OPTS = frozenset({'-e', '-f', '--regexp', '--file'})
_HEAD_TAIL_VALUE_OPTS = frozenset({'-n', '-c', '--lines', '--bytes'})
_SED_RANGE_RE = re.compile(r'^(\d+)(?:,(\d+))?p$')
_NOT_LITERAL_RE = re.compile(r'[$`*?<>{}\[\]]')


def _literal_path(word: str) -> bool:
    return bool(word) and not word.startswith('-') and not _NOT_LITERAL_RE.search(word)


def _resolve(cwd: str, word: str) -> str:
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(word)))


def _operands(args: list[str], value_opts: frozenset[str]) -> list[str]:
    """Non-option words, skipping the value that follows a value-taking option."""
    found: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in value_opts:
            skip = True
        elif not arg.startswith('-'):
            found.append(arg)
    return found


def _head_count(args: list[str]) -> int | None:
    for index, arg in enumerate(args):
        if arg in ('-n', '--lines') and index + 1 < len(args) and args[index + 1].isdigit():
            return int(args[index + 1])
        if re.fullmatch(r'-\d+', arg):
            return int(arg[1:])
        match = re.fullmatch(r'-n(\d+)', arg)
        if match:
            return int(match.group(1))
    return None


def _segment_reads(program: str, args: list[str], cwd: str) -> list[BashRead]:
    if program in _WHOLE_FILE_VIEWERS:
        return [BashRead(_resolve(cwd, word), None, None, True)
                for word in _operands(args, frozenset()) if _literal_path(word)]
    if program in ('head', 'tail'):
        count = _head_count(args) if program == 'head' else None
        return [BashRead(_resolve(cwd, word), 1 if count else None, count, False)
                for word in _operands(args, _HEAD_TAIL_VALUE_OPTS) if _literal_path(word)]
    if program == 'sed':
        if any(arg == '-i' or arg.startswith('-i') or arg == '--in-place' for arg in args):
            return []  # an edit, recorded from the result's diff, not a view
        operands = _operands(args, frozenset({'-e', '-f'}))
        if len(operands) < 2:
            return []
        script, files = operands[0], operands[1:]
        match = _SED_RANGE_RE.match(script) if '-n' in args else None
        start = int(match.group(1)) if match else None
        end = int(match.group(2) or match.group(1)) if match else None
        whole = '-n' not in args
        return [BashRead(_resolve(cwd, word), start, end, whole)
                for word in files if _literal_path(word)]
    if program in _GREPS:
        if any(arg == '--recursive' or re.fullmatch(r'-[A-Za-z]*[rR][A-Za-z]*', arg) for arg in args):
            return []  # a tree search names no single file
        operands = _operands(args, _GREP_VALUE_OPTS)
        has_pattern_opt = any(arg in _GREP_PATTERN_OPTS for arg in args)
        files = operands if has_pattern_opt else operands[1:]
        return [BashRead(_resolve(cwd, word), None, None, False)
                for word in files if _literal_path(word) and os.path.splitext(word)[1]]
    return []


def bash_reads(command: str, cwd: str) -> list[BashRead] | None:
    """File views a Bash command made. None when the command does not
    tokenize, so the caller can count it instead of treating it as "read
    nothing"."""
    segments = playbook_core.shell_segments(playbook_core.strip_heredoc_bodies(command))
    if segments is None:
        return None
    reads: list[BashRead] = []
    current = cwd
    for segment in segments:
        words = playbook_core.command_words(segment)
        if not words:
            continue
        program = words[0].rsplit('/', 1)[-1]
        if program == 'cd':
            if len(words) > 1 and _literal_path(words[1]):
                current = _resolve(current, words[1])
            continue
        reads.extend(_segment_reads(program, words[1:], current))
    return reads


_TEST_PATH_RE = re.compile(r'(^|/)(tests?|specs?|e2e)(/|$)|(^|/)test_|_test\.|\.(test|spec)\.|run_all')


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


_SCRIPT_NAME_RE = re.compile(r'(^|:)(test|tests|lint|build|typecheck|type-check|check|e2e|verify|tsc)(:|$)')
_STANDALONE = frozenset({
    'pytest', 'tox', 'nox', 'mypy', 'pyright', 'flake8', 'pylint', 'tsc', 'jest', 'vitest',
    'eslint', 'mvn', 'mvnw', 'gradle', 'gradlew', 'ctest', 'phpunit', 'rspec',
})
_SUBCOMMANDS = {
    'go': frozenset({'test', 'build', 'vet'}),
    'cargo': frozenset({'test', 'build', 'check', 'clippy'}),
    'dotnet': frozenset({'test', 'build'}),
    'ruff': frozenset({'check'}),
    'biome': frozenset({'check', 'lint', 'ci'}),
    'make': frozenset({'test', 'check', 'lint', 'build', 'verify'}),
}
_PYTHON_MODULES = frozenset({'pytest', 'unittest', 'mypy', 'ruff', 'tox', 'nox', 'pyright'})
_PACKAGE_RUNNERS = frozenset({'npm', 'yarn', 'pnpm', 'bun'})


def _segment_validation(words: list[str]) -> str | None:
    program = words[0].rsplit('/', 1)[-1]
    args = words[1:]
    if program in ('npx', 'bunx', 'pnpx') and args:
        program, args = args[0].rsplit('/', 1)[-1], args[1:]
    if program in _STANDALONE:
        return program
    if program in _SUBCOMMANDS:
        sub = next((arg for arg in args if not arg.startswith('-')), '')
        return f'{program} {sub}' if sub in _SUBCOMMANDS[program] else None
    if re.fullmatch(r'python[\d.]*|py', program):
        if '-m' in args and args.index('-m') + 1 < len(args):
            module = args[args.index('-m') + 1]
            return f'python -m {module}' if module in _PYTHON_MODULES else None
        script = next((arg for arg in args if not arg.startswith('-')), '')
        return f'python {script}' if _TEST_PATH_RE.search(script) else None
    if program in _PACKAGE_RUNNERS:
        names = [arg for arg in args if not arg.startswith('-') and arg not in ('run', 'run-script')]
        script = names[0] if names else ''
        return f'{program} {script}' if _SCRIPT_NAME_RE.search(script) else None
    if program in ('bash', 'sh', 'zsh'):
        script = next((arg for arg in args if not arg.startswith('-')), '')
        return f'sh {script}' if _TEST_PATH_RE.search(script) else None
    return None


def validation_key(command: str) -> str | None:
    """Which check a Bash command ran (test, build, lint, type check), as a
    key that is the same for a re-run of that check; None when it ran none.

    The command word decides, never a bare word anywhere in the text: `grep
    tsc package.json` is not a type check."""
    segments = playbook_core.shell_segments(playbook_core.strip_heredoc_bodies(command))
    for segment in segments or []:
        words = playbook_core.command_words(segment)
        if words:
            key = _segment_validation(words)
            if key:
                return key
    return None


# The verdict line a failing check prints. Counts must be non-zero: "0 errors"
# and "0 failed" are a pass.
_OUTPUT_FAILED_RE = re.compile(
    r'FAILED \(|\b[1-9]\d* (failed|failing|errors?|problems?)\b|BUILD FAIL(URE|ED)|'
    r'(Failures|Errors): [1-9]|error TS\d+|error\[E\d+\]|npm ERR!|^--- FAIL|^FAIL\b|'
    r'Traceback \(most recent call last\)', re.M)


def output_failed(tail: str) -> bool:
    """Whether the end of a check's output reports a failure. Needed because
    most checks are run piped (`... | tail -5`), and a pipeline's exit status
    is its last command's."""
    return bool(_OUTPUT_FAILED_RE.search(tail or ''))


# First match wins: the specific classes come before the generic ones.
_ERROR_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('timeout', re.compile(r'timed out|TimeoutError|TimeoutExpired|deadline exceeded', re.I)),
    ('syntax', re.compile(r'SyntaxError|IndentationError|unexpected token|parse error|'
                          r'unterminated|expected [\'"`]?[;:)}\]]', re.I)),
    ('import_or_name', re.compile(r'ModuleNotFoundError|ImportError|NameError|cannot find (module|name|symbol)|'
                                  r'is not defined|unresolved (import|reference)|undefined: |'
                                  r'no such file or directory|has no attribute', re.I)),
    ('type', re.compile(r'\bTS\d{4}\b|TypeError|type mismatch|incompatible types?|mismatched types|'
                        r'is not assignable|\bmypy\b|cannot be applied to', re.I)),
    ('assertion', re.compile(r'AssertionError|\bFAIL(ED|URE)?\b|expected .{0,80}(got|received|but was)|'
                             r'\d+ (failed|failing)|assert', re.I)),
    ('lint', re.compile(r'checkstyle|\blint\b', re.I)),
    ('runtime', re.compile(r'Traceback|Exception|panic:|Segmentation fault|\bError\b', re.I)),
)


_LINTERS = frozenset({'ruff', 'eslint', 'biome', 'flake8', 'pylint'})


def error_class(text: str, key: str = '') -> str:
    """The kind of failure a validation output shows; 'unknown' when no class
    matches, so an unrecognised failure is counted, not hidden. A linter's
    failure is a lint failure whatever its output looks like."""
    if key.split(' ')[0] in _LINTERS:
        return 'lint'
    for name, pattern in _ERROR_CLASSES:
        if pattern.search(text or ''):
            return name
    return 'unknown'
