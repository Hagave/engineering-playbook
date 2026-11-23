#!/usr/bin/env python3
"""
Provider-agnostic primitives shared by the Claude Code guard
(hooks/context_guard.py) and the Codex adapter guards
(hooks/codex_context_guard.py, hooks/codex_stop.py).

Functions and constants, plus the state, journal and preference files the
hooks share (written atomically) and the git commands the gates read — no
dependency on either provider's transcript JSON schema or hook I/O contract.
Both deny a PreToolUse call with exit 0 and a permissionDecision "deny"
payload, but the payloads differ in detail (see hooks/codex_context_guard.py),
and each provider's own hook script owns that translation; this module is the single source of truth for the checks and
state format that must behave identically across both, so a fix here does
not need to be manually re-ported to a second implementation.
"""

import ast
import contextlib
import fnmatch
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import warnings
from enum import Enum, auto
from typing import NamedTuple

import playbook_i18n
import secret_scan

SPEC_TOKEN = '[SPEC CONFIRMED]'

# Source extensions for every language the playbook governs. The core rules are
# language-agnostic, so the gates must be too — an extension missing here silently
# disables the skill gate, the spec gate and the validation gate for that language.
_CODE_EXT = (
    r'\.(?:tsx|ts|mts|cts|jsx|js|mjs|cjs|vue|svelte'
    r'|pyi|py|rb|php|go|java|kts|kt|scala|sc|swift|dart|rs|cs|fs'
    r'|exs|ex|erl|clj|cljs|hs|lua|zig|sh|bash|zsh'
    r'|cpp|cxx|cc|hpp|hxx|mm|c|h|m)\b'
)
CODE_EXT_RE = re.compile(_CODE_EXT, re.IGNORECASE)

# Bash commands that write source files — these must go
# through the same gates as Write/Edit, otherwise the spec gate is bypassable
# via `tee` or shell redirection. `sed -i` is read per simple command in
# `bash_write_targets`: as a pattern over the whole line it took the `-i` of a
# LATER command (`sed -n 1,9p a.py; grep -i x b.py`) for sed's in-place flag.
BASH_CODE_WRITE_RE = re.compile(
    r"(?:>>?\s*['\"]?\S*{e}"
    r"|\btee\s+(?:-a\s+)?['\"]?\S*{e})".format(e=_CODE_EXT),
    re.IGNORECASE,
)

# The same forms with the TARGET captured. A gate that knows the extension
# but not the DESTINATION cannot tell project source from a throwaway script, and
# it fires on both — see `is_throwaway_write` for the two measured false positives.
BASH_CODE_WRITE_TARGET_RE = re.compile(
    r"(?:>>?\s*['\"]?(\S*{e})"
    r"|\btee\s+(?:-a\s+)?['\"]?(\S*{e}))".format(e=_CODE_EXT),
    re.IGNORECASE,
)

# Redirect operators, read by `redirect_code_targets` with the shell's quoting:
# `>` `>>` `>|` `&>` `&>>` `<>` and `>&` followed by a file. `>&2` and `2>&1`
# duplicate a descriptor and write no file.
_REDIRECT_PUNCT = ';&|()<>'
_TEE_VALUE_OPTS = frozenset({'--output-error'})


def redirect_code_targets(command: str, source_only: bool = True) -> list[str] | None:
    """Source files `command` writes through a redirect or `tee`, as written;
    None when it does not tokenize (the text patterns then apply, failing
    closed). With `source_only` False, every file it writes that way, source or
    not, except through a nested shell script (`bash -c`), whose targets are
    read for source only.

    Read as the shell reads it, not as text: a quoted `->` in a commit message
    (`git commit -m "rename a.py -> b.py"`) or a `>` inside an echoed string is
    no redirect, `tee out.py.log` writes `out.py.log`, and `tee b.txt a.py`
    writes both files."""
    lexer = shlex.shlex(command, posix=False, punctuation_chars=_REDIRECT_PUNCT)
    lexer.whitespace_split = True
    lexer.commenters = ''
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    targets: list[str] = []
    for at, token in enumerate(tokens[:-1]):
        if '>' not in token or not set(token) <= set(_REDIRECT_PUNCT):
            continue
        target = tokens[at + 1]
        if token.endswith('&') and re.fullmatch(r'\d+|-', target):
            continue  # descriptor duplication
        targets.append(target.strip('"\''))
    for segment in shell_segments(command) or ():
        words = command_words(segment)
        if not words:
            continue
        if words[0].rsplit('/', 1)[-1] == 'tee':
            operands = iter(words[1:])
            for word in operands:
                if word in _TEE_VALUE_OPTS:
                    next(operands, None)
                elif not word.startswith('-'):
                    targets.append(word)
        # A shell's -c script and eval's arguments are quoted here, so their
        # redirects are read from the script itself (`bash -c "echo x > a.py"`).
        for script in _nested_scripts(words):
            targets += _redirect_or_text_targets(script)
    return [t for t in targets if not source_only or _ends_with_code_ext(t)]


def _ends_with_code_ext(path: str) -> bool:
    """`out.py.log` names a log, not Python: the extension is the last one."""
    return bool(re.search(_CODE_EXT + '$', path, re.IGNORECASE))


# Text-only fallbacks for a command that does not tokenize (an unbalanced quote):
# no simple commands to read, so any `sed ... -i` on the line counts — the second
# one only when a source file follows it.
_SED_IN_PLACE_RE = re.compile(r'\bsed\s+(?:[^\n]*\s)?-(?:i\b|-in-place\b)')
_SED_IN_PLACE_TEXT_RE = re.compile(
    _SED_IN_PLACE_RE.pattern + r'[^\n]*' + _CODE_EXT, re.IGNORECASE)

# `cd <dir> && <write>` is the ordinary way a one-off script gets written somewhere
# else; without this the target resolves against the wrong root. Only a LITERAL
# directory counts: `cd $VAR` is unresolvable here, and an unresolvable target is
# treated as project source (fail closed — a gate that guesses is a bypass).
_CD_PREFIX_RE = re.compile(r"""^\s*cd\s+(?P<dir>"[^"]*"|'[^']*'|[^\s;&|<>]+)\s*(?:&&|;)""")

CODE_WRITE_TOOLS = ('Write', 'Edit', 'MultiEdit', 'ApplyPatch')


def code_write_targets(tool_name: str, tool_input: dict, command: str = '') -> list[str]:
    """Absolute paths a write would land on, for the gates that need to know WHERE.

    For an editing tool that is its file path; for Bash it is every redirect and
    `tee` target carrying a source extension, plus the destinations of the
    commands in `bash_write_targets` (copy, move, link, download, in-place edit,
    interpreter one-liner). A relative target resolves against a literal leading
    `cd`, else against `cwd`. Returns [] when nothing with a source extension is
    being written.
    """
    cwd = str(tool_input.get('_cwd') or '') or os.getcwd()
    if tool_name in CODE_WRITE_TOOLS:
        path = str(tool_input.get('file_path') or tool_input.get('path')
                   or tool_input.get('target_file') or '')
        if not path or not CODE_EXT_RE.search(path):
            return []
        return [os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))]

    if tool_name != 'Bash':
        return []
    root = _command_root(command, cwd) or cwd
    raw_command = str(tool_input.get('command') or command or '')
    out = []
    # The redirects of the command, and of the command lines its interpreter
    # one-liners run (`python3 -c "os.system('echo x > a.py')"`): quoted, those
    # are no redirect of the outer command, as the text pattern once read them.
    redirects = _redirect_or_text_targets(command or '')
    for line in interpreter_shell_commands(raw_command, cwd):
        redirects += _redirect_or_text_targets(line)
    for raw in redirects:
        out.append(os.path.normpath(os.path.join(root, os.path.expanduser(raw))))
    # Scripts run from the command are read from the raw command: their heredoc
    # bodies are what `command` has stripped.
    scripts = script_write_targets(raw_command, cwd)[0]
    for raw in bash_write_targets(command or '')[0] + scripts:
        out.append(os.path.normpath(os.path.join(root, os.path.expanduser(raw))))
    return out


def _redirect_or_text_targets(command: str) -> list[str]:
    """`redirect_code_targets`, or the text pattern's captures when the
    command does not tokenize."""
    targets = redirect_code_targets(command)
    if targets is not None:
        return targets
    return [t for groups in BASH_CODE_WRITE_TARGET_RE.findall(command)
            for t in groups if t]


def _temp_roots() -> list[str]:
    roots = [tempfile.gettempdir(), '/tmp', '/var/tmp', os.environ.get('TMPDIR', '')]
    return [os.path.realpath(r) for r in roots if r]


def is_throwaway_write(path: str, cwd: str) -> bool:
    """True when a write lands somewhere this project does not keep.

    Both cases below were false positives of the spec and validation gates, and both
    are about the DESTINATION, never the extension:

      - **a temp/scratchpad path** (the session scratchpad lives under the system temp
        dir): a one-off measuring script that is not part of any project. A file
        outside the project but NOT in temp still counts as source — editing another
        repo's code is real work, it just cannot be validated by this project.
      - **a git-IGNORED path inside the project** (`scratch/`, build output): the repo
        itself declares it is not source, and the gates exist to serve the repo's own
        definition, not the file extension's.

    Fails CLOSED: an unresolvable path, a `$VAR` in it, no git, or any error means the
    write is treated as project source. A gate that guesses in the permissive
    direction is a bypass, so the cost of being wrong here is one extra token, never
    a skipped review.
    """
    if not path or '$' in path or '`' in path:
        return False
    real = os.path.realpath(path)
    root_cwd = os.path.realpath(cwd) if cwd else ''

    # INSIDE the project the repo's own ignore list decides, and it decides alone —
    # checking temp first would swallow a whole project that happens to live under a
    # temp root (every test fixture does, and so does anyone working in a scratch
    # clone). This ordering is not cosmetic: it was six failing tests.
    if root_cwd and (real == root_cwd or real.startswith(root_cwd + os.sep)):
        try:
            import subprocess
            done = subprocess.run(['git', '-C', root_cwd, 'check-ignore', '-q', real],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=3)
            return done.returncode == 0
        except Exception:
            return False   # no git, no ignore list, no exemption

    # OUTSIDE the project: only a temp/scratchpad path is throwaway. Another repo's
    # source is still source — it just cannot be validated by this project.
    return any(real == root or real.startswith(root + os.sep) for root in _temp_roots())


def is_code_write(tool_name: str, tool_input: dict, command: str = '', cwd: str = '') -> bool:
    """Whether this call writes source the project KEEPS — the signal every gate uses.

    Extension alone is not enough (it fired on a probe in `scratch/` and on a parser in
    the session scratchpad); a write counts only when at least one destination is not
    throwaway. A write with no resolvable target counts, so the gates keep firing when
    the destination cannot be read.
    """
    targets = code_write_targets(tool_name, {**tool_input, '_cwd': cwd}, command)
    if not targets:
        if tool_name in CODE_WRITE_TOOLS:
            return False
        # A write whose destination only the command itself knows (`git apply`,
        # `patch`, `git checkout <ref> -- .`) counts: an unresolvable target is
        # project source, the rule `is_throwaway_write` follows too.
        return bool(command) and (
            (redirect_code_targets(command) is None and bool(BASH_CODE_WRITE_RE.search(command)))
            or bash_write_targets(command)[1]
            or script_write_targets(str(tool_input.get('command') or command), cwd)[1])
    base = cwd or os.getcwd()
    return any(not is_throwaway_write(t, base) for t in targets)

# `rm -r` and `xargs rm` are read from the tokens of each simple command
# (`token_deletion`): as text, `\brm` matched the `rm` of `docker run --rm` and
# the `r` of a later flag (`javac -proc`) read as `-r`. These patterns are the
# fallback for text that does not tokenize, where failing closed is the point.
DELETION_TEXT_RULES = [
    (r'\brm\s+(?:[^\n]*\s)?(?:-[a-zA-Z]*[rR]|--recursive\b)', 'delete_tree'),
    (r'\bxargs\b[^\n]*\brm\b', 'mass_delete'),
]

DESTRUCTIVE_RULES = [
    (r'\bgit\s+push\b[^\n]*(?:\s-f\b|\s--force\b|\s\+\S+)', 'force_push'),
    (r'\bgit\s+reset\s+--hard\b', 'reset_hard'),
    # Forced branch deletion in every spelling: -D, a -df/-fd cluster, and the
    # long forms in either order. A plain -d refuses an unmerged branch.
    # Case-sensitive on the short flags: the patterns run with IGNORECASE, and
    # read that way `-d` (safe delete) was `-D`.
    (r'\bgit\s+branch\b[^\n;&|]*?\s(?:(?-i:-[a-zA-Z]*D)\b'
     r'|(?-i:-[a-zA-Z]*(?:d[a-zA-Z]*f|f[a-zA-Z]*d)[a-zA-Z]*)\b'
     r'|(?:--delete|-d)\b[^\n;&|]*\s(?:--force|-f)\b'
     r'|(?:--force|-f)\b[^\n;&|]*\s(?:--delete|-d)\b)', 'branch_delete'),
    (r'\bgit\s+checkout\s+--\s', 'discard_changes'),
    # `git restore` discards working-tree changes unless it only unstages.
    # Short flags case-sensitive: under IGNORECASE `-s` (--source, which
    # overwrites the working tree) read as `-S` (--staged, which only unstages).
    (r'\bgit\s+restore\b(?=[^\n;&|]*\s(?:--worktree|(?-i:-W))\b)', 'discard_changes'),
    (r'\bgit\s+restore\b(?![^\n;&|]*\s(?:--staged|(?-i:-S))\b)', 'discard_changes'),
    (r'\bgit\s+clean\s+(?:[^\n]*\s)?(?:-[a-zA-Z]*[fdx]\b|--force\b)', 'clean'),
    # History and stash that no reflog brings back.
    (r'\bgit\s+stash\s+(?:clear|drop)\b', 'stash_drop'),
    (r'\bgit\s+reflog\s+(?:expire|delete)\b', 'history'),
    (r'\bgit\s+update-ref\s+(?:[^\n;&|]*\s)?-d\b', 'history'),
    (r'\bgit\s+filter-(?:branch|repo)\b', 'history'),
    # A forced worktree removal discards its uncommitted changes.
    (r'\bgit\s+worktree\s+remove\b[^\n;&|]*\s(?:--force|-f)\b', 'worktree_remove'),
    (r'\brsync\b[^\n;&|]*\s--delete', 'sync_delete'),
    (r'\bshred\s', 'shred'),
    (r'\btruncate\s+[^\n;&|]*(?:-s\s*0+\b|--size(?:=|\s+)0+\b)', 'truncate'),
    (r'\bfind\s+[^\n]*-delete\b', 'mass_delete'),
    (r'\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\s+(?:IF\s+EXISTS\s+)?[`"\[]?\w', 'sql'),
    (r'\bTRUNCATE\s+(?:TABLE\s+|ONLY\s+)?[`"\[]?\w+[`"\]]?\s*(?:;|$|\s+(?:CASCADE|RESTART))', 'sql'),
    (r'(?s)\bDELETE\s+FROM\b(?!.*\bWHERE\b)', 'sql'),
]
DESTRUCTIVE_PATTERNS = [pattern for pattern, _ in DESTRUCTIVE_RULES]

# AI attribution on anything a team reads as project history. The harness injects
# two forms by default -- the commit trailer and a "Generated with Claude Code"
# line in a pull request body -- and a policy against AI attribution means both.
#
# Keyed on the address as well as the name: a trailer that names another model,
# or no model at all, still carries the noreply address.
#
# One rule for every harness: the Antigravity commit-msg hook calls
# scripts/check_commit_message.py, which reads this pattern, so the agents those
# harnesses run (Gemini, Copilot) are named here too.
AI_ATTRIBUTION_RE = re.compile(
    r'co-authored-by:[^\n]*(?:claude|anthropic|gemini|antigravity|copilot)'
    r'|noreply@anthropic\.com'
    r'|generated\s+(?:with|by)\s+\[?claude',
    re.IGNORECASE,
)

# The same three alternatives as POSIX ERE for `git log --grep`, which matches
# line by line (so `[^\n]*` becomes `.*`) and is case-folded by `-i`. Letting
# git do the search keeps a first push of a long history inside the hook timeout.
_AI_ATTRIBUTION_GREP = (
    'co-authored-by:.*(claude|anthropic|gemini|antigravity|copilot)',
    r'noreply@anthropic\.com',
    r'generated[[:space:]]+(with|by)[[:space:]]+\[?claude',
)

# The only exception, and it is the user's to set: a phrase the model writes
# can never stand in for it.
ATTRIBUTION_ALLOW_ENV = 'PLAYBOOK_ALLOW_AI_ATTRIBUTION'

# `git` plus any global options before the subcommand (`-C dir`, `-c k=v`,
# `--no-pager`, `--git-dir=...`). Accepting only `-C` let `git -c x=y push`
# past every gate keyed on the subcommand.
_GIT_PREFIX = r'\bgit\s+(?:(?:-[Cc]\s+\S+|--[\w-]+(?:=\S+)?)\s+)*'

# Commands that publish a message into project history. `git commit` used to be
# the only one checked, so a trailer kept out of the commit still reached the
# team through the pull request body -- and, on a single-commit PR, the forge
# pre-fills that body FROM the commit message, which is how one shipped.
PUBLISH_CMD_RE = re.compile(
    _GIT_PREFIX + r'(?:commit|merge|tag|notes)\b'
    r'|\bgh\s+(?:pr|issue)\s+(?:create|edit|comment)\b'
    r'|\bglab\s+mr\s+(?:create|update|note)\b'
    r'|\baz\s+repos\s+pr\s+(?:create|update)\b',
    re.IGNORECASE,
)

# Command words whose heredoc body IS the text under inspection -- a commit
# message or a pull request body fed through `<<EOF`. Every other heredoc is
# incidental data and stays stripped. `gh`/`glab`/`az` are here because the
# attribution check reads pull request bodies too, and those arrive almost
# exclusively as `--body "$(cat <<'EOF' ...)"`.
PUBLISH_TOOLS = frozenset({'git', 'gh', 'glab', 'az'})

# A heredoc body written by cat/tee, or fed to a non-shell interpreter, is data
# or foreign-language source — not shell about to run. Scanning it with shell and
# SQL patterns is a category error, and it is the main source of destructive-block
# false positives: writing a doc, a regex or a test fixture that merely mentions a
# dangerous command was blocked as if it were executing it.
_HEREDOC_START_RE = re.compile(r"""<<-?\s*(['"]?)(\w+)\1""")
_SEGMENT_SEP_RE = re.compile(r'[|;&\n]')

# Command words that execute their heredoc as shell. Everything else — cat, tee,
# python3, node, jq, ssh-less tools — receives it as data or as another
# language's source, which these shell and SQL patterns cannot meaningfully match.
_SHELL_CONSUMERS = frozenset({'sh', 'bash', 'zsh', 'ksh', 'dash', 'ash',
                              'eval', 'source', '.', 'ssh', 'sudo', 'doas'})


def _segment_command_word(command: str, upto: int) -> str:
    """The command word of the pipeline segment ending at `upto`."""
    start = 0
    for match in _SEGMENT_SEP_RE.finditer(command, 0, upto):
        start = match.end()
    for word in command[start:upto].split():
        if '=' in word and not word.startswith('-'):
            continue  # leading env assignment, e.g. FOO=bar cmd <<EOF
        return word.rsplit('/', 1)[-1]
    return ''


def strip_heredoc_bodies(command: str, keep_for: frozenset | set = frozenset()) -> str:
    """Command with non-shell heredoc payloads replaced by a placeholder.

    `keep_for` names extra command words whose heredoc body must be preserved
    because the body is the subject of the check, not incidental data — the
    commit-attribution check passes {'git'} so that `git commit -F - <<EOF`
    still has its message read, while a python heredoc that merely quotes a
    commit message does not.

    A heredoc fed to cat, tee or a non-shell interpreter is data or foreign
    source, not shell about to run, and scanning it for shell and SQL patterns
    is a category error. It is also the main source of destructive-block false
    positives: writing a doc, a regex or a test fixture that merely mentions a
    dangerous command was blocked as if it were executing it. A heredoc fed to a
    shell is kept — there the body really is about to run."""
    result = command
    consumers = _SHELL_CONSUMERS | set(keep_for)
    pos = 0
    # Walk the heredocs in order on the text being rewritten. Matching each
    # opener against the ORIGINAL text and substituting the first occurrence
    # left every later heredoc with the same delimiter (`cat > a <<EOF ... EOF;
    # cat > b <<EOF ... EOF`) unstripped: the first, already-stripped body was
    # matched again each time.
    while True:
        match = _HEREDOC_START_RE.search(result, pos)
        if not match:
            return result
        pos = match.end()
        if _segment_command_word(result, match.start()) in consumers:
            continue
        body = _heredoc_body_re(match).match(result, match.start())
        if not body:
            continue
        replacement = body.group(1) + '[heredoc body omitted]' + body.group(3)
        result = result[:body.start()] + replacement + result[body.end():]
        pos = body.start() + len(replacement)


def _heredoc_body_re(start: re.Match[str]) -> re.Pattern[str]:
    """Opening line (1), body (2) and closing delimiter (3) of the heredoc
    whose `<<WORD` is `start`."""
    return re.compile(
        r'(' + re.escape(start.group(0)) + r'[^\n]*\n)(.*?)(\n[ \t]*'
        + re.escape(start.group(2)) + r'\b)',
        re.DOTALL,
    )


# Interpreter payloads passed inline with -c/-e/-r are another language's source,
# not shell about to run -- the same category error strip_heredoc_bodies fixes for
# heredocs, one surface further. Measured live: a `python3 -c` one-liner whose
# string literal merely mentioned a push command tripped the review gate, and the
# command was not a push at all. A shell's own `-c` is deliberately absent from
# the interpreter list, because there the payload IS shell about to run.
_INLINE_PAYLOAD_RE = re.compile(
    r"(\b(?:python3?|node|ruby|perl|php)\s+(?:[^\s;|&]+\s+)*?-[ceprn]\s+)"
    r"""(?:\$'(?:[^'\\]|\\.)*'|'[^']*'|"(?:[^"\\]|\\.)*")"""
)


def strip_inline_payloads(command: str) -> str:
    """Command with inline interpreter scripts replaced by a placeholder.

    Only a quoted payload is stripped, because only a quoted payload has an
    unambiguous end; an unquoted one is left alone rather than guessed at."""
    return _INLINE_PAYLOAD_RE.sub(r"\1'[inline script omitted]'", command)


# ─── Shell structure ─────────────────────────────────────────────────────────
#
# The gates match patterns against command TEXT, and text is what an evasion
# changes: `'rm' -rf`, `r''m -rf`, `git -C . reset --hard` all run the same
# thing as the spelling the pattern knows. These helpers recover the structure
# the shell itself sees — simple commands, quotes removed, wrappers and git's
# global options dropped — so the patterns also run against that form. They
# are heuristics over a language they do not fully parse: when the text does
# not tokenize they return nothing and the raw-text patterns still apply.

_SHELL_PUNCT = ';&|()'
_ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
# Words that run the command after them; their own options are skipped too.
_WRAPPER_WORDS = frozenset({'sudo', 'doas', 'env', 'command', 'builtin', 'exec', 'nohup',
                            'time', 'nice', 'timeout', 'stdbuf', 'xargs'})
# Per wrapper, the options that take the next word as their value. Shared
# across wrappers they misread: `env -i` takes none, `stdbuf -i` does.
_WRAPPER_VALUE_OPTS = {
    'sudo': frozenset({'-u', '-g', '-C', '-p', '-h', '-U'}),
    'doas': frozenset({'-u', '-C'}),
    'env': frozenset({'-u', '-C', '-S'}),
    'nice': frozenset({'-n'}),
    'timeout': frozenset({'-s', '-k'}),
    'stdbuf': frozenset({'-i', '-o', '-e'}),
    'xargs': frozenset({'-I', '-n', '-P', '-L', '-d', '-E', '-s', '-a'}),
}
_SHELL_WORDS = frozenset({'sh', 'bash', 'zsh', 'ksh', 'dash', 'ash'})
# Keywords that come before the command of a compound statement (`do rm -rf x`).
_SHELL_KEYWORDS = frozenset({'do', 'then', 'else', 'elif', '!', '{', '}'})
_GIT_GLOBAL_OPTS_RE = re.compile(
    r'\bgit\s+(?:(?:-[Cc]\s+\S+'
    r'|--(?:git-dir|work-tree|namespace|exec-path|super-prefix|config-env)(?:=|\s+)\S+'
    r'|--[\w-]+(?:=\S+)?|-[pP])\s+)+')


def _unquoted_newlines_as_separators(command: str) -> str:
    """Newlines outside quotes end a command, as `;` does; inside quotes they
    are part of a word (a multi-line commit message)."""
    out: list[str] = []
    quote = ''
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
        elif ch == '\\' and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = ''
        elif ch in '\'"':
            quote = ch
        elif ch == '\n':
            out.append(' ; ')
            continue
        out.append(ch)
    return ''.join(out)


_SUBSTITUTION_WORD = '$SUBST'


def _closing_paren(text: str, open_at: int) -> int | None:
    """Index of the `)` closing the `(` at `open_at`, skipping quoted text and
    nested parentheses; None when it is never closed."""
    depth = 0
    quote = ''
    i = open_at
    while i < len(text):
        ch = text[i]
        if ch == '\\' and quote != "'":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ''
        elif ch in '\'"':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _lift_substitutions(command: str) -> tuple[str, list[str]]:
    """`command` with each `$(...)` and `` `...` `` replaced by one placeholder
    word, plus the commands they contained.

    A substitution is a command of its own, and its parentheses are not
    command separators: read as one, `git diff $(git merge-base a $B) $B`
    split into a "command" starting with `$B`, and a quote nested inside
    `"$(grep "x" .)"` broke the tokenizer for the whole line."""
    out: list[str] = []
    inner: list[str] = []
    quote = ''
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == '\\' and quote != "'":
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote == "'":
            quote = '' if ch == "'" else quote
        elif ch == '$' and command[i + 1:i + 3] == '((' and _closing_paren(command, i + 1):
            # Arithmetic expansion: a value, not a command. Read as `$(` it
            # made `(... -6)` a command whose program was `$SUBST-6`, and
            # `sed -n "$(($(grep -n x f | cut -d: -f1)-6)),\$p" f` was
            # blocked as decided at run time. The substitutions inside it
            # are still commands of their own.
            end = _closing_paren(command, i + 1)
            inner += _lift_substitutions(command[i + 3:end - 1])[1]
            out.append(_SUBSTITUTION_WORD)
            i = end + 1
            continue
        elif ch == '$' and command[i + 1:i + 2] == '(':
            end = _closing_paren(command, i + 1)
            if end is None:
                out.append(command[i:])
                break
            inner.append(command[i + 2:end])
            out.append(_SUBSTITUTION_WORD)
            i = end + 1
            continue
        elif ch == '`':
            end = command.find('`', i + 1)
            if end == -1:
                out.append(command[i:])
                break
            inner.append(command[i + 1:end])
            out.append(_SUBSTITUTION_WORD)
            i = end + 1
            continue
        elif ch == '"':
            quote = '' if quote == '"' else '"'
        elif ch == "'" and not quote:
            quote = "'"
        out.append(ch)
        i += 1
    return ''.join(out), inner


def shell_segments(command: str) -> list[list[str]] | None:
    """Simple commands of `command`, each as its words with quotes removed;
    commands inside `$(...)` and backticks are segments of their own, and the
    substitution itself reads as the word `$SUBST`.

    None when the text does not tokenize (an unbalanced quote)."""
    outer, inner = _lift_substitutions(command.replace('\\\n', ' '))
    segments = _tokenize_segments(outer)
    if segments is None:
        return None
    for nested in inner:
        segments += shell_segments(nested) or []
    return segments


def _statements(command: str) -> list[tuple[str, list[str]]] | None:
    """Simple commands of `command` (substitutions already lifted), each with
    the separator token that comes before it ('' for the first)."""
    text = _unquoted_newlines_as_separators(command)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_SHELL_PUNCT)
    lexer.whitespace_split = True
    lexer.commenters = ''
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    statements: list[tuple[str, list[str]]] = []
    current: list[str] = []
    separator = ''
    for token in tokens:
        # Inside `[[ ... ]]`, `&&`, `||` and parentheses are operators of the
        # test, not command separators: split there, `[[ "$t" > a && "$t" < b ]]`
        # made `$t < b ]]` a command whose program was decided at run time.
        in_test = bool(current) and current[0] == '[[' and current[-1] != ']]'
        if token and all(ch in _SHELL_PUNCT for ch in token) and not in_test:
            if current:
                statements.append((separator, current))
            current = []
            separator = token
        else:
            current.append(token)
    if current:
        statements.append((separator, current))
    return statements


def _tokenize_segments(command: str) -> list[list[str]] | None:
    statements = _statements(command)
    return None if statements is None else [words for _, words in statements]


_VAR_REF_RE = re.compile(r'\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))')
# A value with any of these is not one fixed text once the shell expands it.
_NON_LITERAL_VALUE_CHARS = frozenset(';&|<>()*?[`$')
# Builtins whose arguments are assignments to the running shell.
_DECLARE_WORDS = frozenset({'export', 'local', 'readonly', 'declare', 'typeset'})
# A name with more literal values than this is checked on its first ones.
_CANDIDATES_MAX = 16
_DEFAULT_IFS = ' \t\n'


def _expand_candidates(value: str, candidates: dict[str, list[str]]) -> list[str]:
    """Every text `value` can be with its references replaced by the literal
    values those names take in the command; [] when it holds anything the
    shell computes (a substitution, a glob, a name with no literal value)."""
    texts = ['']
    position = 0
    for match in _VAR_REF_RE.finditer(value):
        name = match.group(1) or match.group(2)
        if not candidates.get(name):
            return []
        prefix = value[position:match.start()]
        texts = [text + prefix + one for text in texts
                 for one in candidates[name]][:_CANDIDATES_MAX]
        position = match.end()
    texts = [text + value[position:] for text in texts]
    return [text for text in texts
            if text.strip() and not any(ch in _NON_LITERAL_VALUE_CHARS for ch in text)]


def _assignments(words: list[str]) -> list[str]:
    """The `NAME=value` words a statement assigns in the running shell: a
    statement of assignments alone, the leading ones of a command (which also
    count: conservatively, since only that command sees them), and the
    arguments of `export`, `local` and the other declaring builtins."""
    while words and words[0] in _SHELL_KEYWORDS:
        words = words[1:]
    at = 1 if words and words[0] in _DECLARE_WORDS else 0
    found = []
    for word in words[at:]:
        if _ENV_ASSIGN_RE.match(word):
            found.append(word)
        elif not (at and word.startswith('-')):
            break
    return found


def _literal_candidates(statements: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    """Every literal value each name takes anywhere in the command: each
    assignment and each word a `for NAME in ...` list holds, wherever it sits
    (a branch, a loop, after `&&`), in any order. Read twice, so a value that
    names a variable assigned later in the text is expanded too."""
    candidates: dict[str, list[str]] = {}
    for _ in range(2):
        for _separator, words in statements:
            pairs = [tuple(word.split('=', 1)) for word in _assignments(words)]
            if len(words) > 2 and words[0] == 'for' and words[2] == 'in':
                pairs += [(words[1], word) for word in words[3:] if word != 'do']
            for name, value in pairs:
                known = candidates.setdefault(name, [])
                for text in _expand_candidates(value, candidates):
                    if text not in known and len(known) < _CANDIDATES_MAX:
                        known.append(text)
    return candidates


def _split_words(value: str, separators: list[str]) -> list[list[str]]:
    """`value` split into words the way each IFS the command may set splits it."""
    shapes = []
    for ifs in separators:
        words = [word for word in re.split('[' + re.escape(ifs) + ']+', value) if word]
        if words and words not in shapes:
            shapes.append(words)
    return shapes


def resolve_literal_commands(command: str) -> str:
    """`command` with each command word that is a variable replaced by every
    literal value the command gives it (`P="docker exec db psql"; $P -c "select
    1"`), one simple command per line and one line per value; `command`
    itself when there is none.

    Every literal the text holds for the name counts, wherever it is assigned
    and in whatever order (`ok && P="rm -rf" || P=echo; $P build` is read as
    both), split by every IFS the command sets. What the text cannot show (a
    value from `$(...)`, `read`, `source`, the environment) is not read: a
    program the command does not show is not asked about (decided by the user
    for the auto-mode consent work), while every one it does show is checked.
    Commands inside substitutions are kept as they are."""
    outer, inner = _lift_substitutions(command.replace('\\\n', ' '))
    statements = _statements(outer)
    if not statements:
        return command
    candidates = _literal_candidates(statements)
    separators = [_DEFAULT_IFS] + candidates.get('IFS', [])
    resolved: list[list[str]] = []
    changed = False
    for _separator, words in statements:
        effective = command_words(words)
        reference = _VAR_REF_RE.fullmatch(effective[0]) if effective else None
        name = (reference.group(1) or reference.group(2)) if reference else ''
        if not candidates.get(name):
            resolved.append(words)
            continue
        prefix = words[:len(words) - len(effective)]
        for value in candidates[name]:
            resolved += [prefix + shape + effective[1:] for shape in _split_words(value, separators)]
        changed = True
    if not changed:
        return command
    return '\n'.join([' '.join(shlex.quote(word) for word in words) for words in resolved]
                     + inner)


def command_words(segment: list[str]) -> list[str]:
    """The words of a simple command from its effective command word on:
    leading `VAR=value` assignments and wrappers (`sudo`, `env`, `command`,
    `nohup`, `timeout 5`...) are skipped."""
    i = 0
    while i < len(segment):
        word = segment[i]
        if _ENV_ASSIGN_RE.match(word) or word in _SHELL_KEYWORDS:
            i += 1
            continue
        base = word.rsplit('/', 1)[-1]
        if base not in _WRAPPER_WORDS:
            return segment[i:]
        i += 1
        value_opts = _WRAPPER_VALUE_OPTS.get(base, frozenset())
        while i < len(segment):
            option = segment[i]
            if option in value_opts:
                i += 2
            elif (option.startswith('-') or _ENV_ASSIGN_RE.match(option)
                  or (base == 'timeout' and re.fullmatch(r'[\d.]+[smhd]?', option))):
                i += 1
            else:
                break
    return []


def normalized_command(command: str) -> str:
    """`command` as the shell runs it: one simple command per line, quotes
    removed, wrappers and git's global options dropped. '' when it does not
    tokenize."""
    segments = shell_segments(command)
    if not segments:
        return ''
    # One command per line, and a bare assignment is not a command: `x=rm` next
    # to `$x -rf` must not read as `rm -rf` (that one is resolve_literal_commands').
    text = '\n'.join(' '.join(words) for words in map(command_words, segments) if words)
    return _GIT_GLOBAL_OPTS_RE.sub('git ', text)


def inline_payloads(command: str) -> list[str]:
    """Quoted `-c`/`-e` scripts handed to an interpreter inside the command.

    Heredoc scripts (`python3 - <<EOF`) and script files are `script_sources`'s:
    Python there is read through its syntax tree, so a script that only
    MENTIONS a deletion API (a test corpus, a doc generator) is not blocked."""
    return [m.group(0)[len(m.group(1)):] for m in _INLINE_PAYLOAD_RE.finditer(command)]


# Recursive deletion through an interpreter API: the `rm -r` of another
# language. Single-file removal is left alone, as `rm <file>` is.
_RECURSIVE_DELETE_API_RE = re.compile(
    r'\bshutil\s*\.\s*rmtree\s*\('
    r'|\b(?:rmSync|rm|rmdirSync|rmdir)\s*\([^)]*\brecursive\s*:\s*true'
    # Node options the text cannot read (a variable, a spread) may hold
    # `recursive: true`; read as absent they let `fs.rmSync(dir, opts)` through.
    r'|\b(?:rmSync|rmdirSync)\s*\(\s*[^,()]+,\s*(?:[A-Za-z_$]|\{\s*\.\.\.)'
    r'|\.(?:rm|rmdir)\s*\(\s*[^,()]+,\s*(?:[A-Za-z_$][\w$.]*\s*[,)]|\{\s*\.\.\.)'
    r'|\bFileUtils\s*\.\s*(?:rm_rf|rm_r|remove_dir|remove_entry(?:_secure)?|rmtree)\b'
    r'|\b(?:rmtree|remove_tree)\s*\(',
)

# ─── Interpreter scripts beyond `-c` ─────────────────────────────────────────
#
# A heredoc fed to an interpreter (`python3 - <<EOF`) and a script file run by
# one (`python3 cleanup.py`, `./build.sh`) do what an inline `-c` script does,
# so the gates read them too. Python is read through its syntax tree: a call
# counts, a string or comment that only MENTIONS `shutil.rmtree` (a test
# corpus, a doc generator) does not. That distinction was the reason heredoc
# scripts used to be left unread.

_SCRIPT_MAX_BYTES = 1_000_000
# How deep a shell script running another script is followed.
_SCRIPT_MAX_DEPTH = 2
_OTHER_INTERPRETERS = frozenset({'node', 'nodejs', 'bun', 'deno', 'ruby', 'perl', 'php'})
_PYTHON_WORD_RE = re.compile(r'python(?:\d+(?:\.\d+)*)?')
# Options after which the interpreter runs no script file: inline source,
# stdin, a module.
_NO_SCRIPT_FILE_OPTS = frozenset({'-c', '-e', '-E', '-m', '-r', '-p', '-', '--eval', '--print',
                                  '--check'})
_INTERPRETER_VALUE_OPTS = frozenset({'-W', '-X', '--require', '-I', '--import', '--loader'})
_SCRIPT_EXT_LANGUAGE = {
    '.py': 'python', '.js': 'other', '.mjs': 'other', '.cjs': 'other', '.ts': 'other',
    '.mts': 'other', '.cts': 'other',
    '.rb': 'other', '.pl': 'other', '.php': 'other', '.sh': 'shell', '.bash': 'shell',
}
_PY_RECURSIVE_DELETE_CALLS = frozenset({'rmtree', 'remove_tree'})
# A file-writing call with a literal path, in a language read as text.
_TEXT_WRITE_CALL_RE = re.compile(
    r"""\b(?:writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream"""
    r"""|File\s*\.\s*(?:write|open))\s*\(\s*['"]([^'"\s]+)['"]""")


class ScriptSource(NamedTuple):
    """A script a command hands an interpreter: its language ('python',
    'other' or 'shell'), where it came from ('heredoc' or the path as
    written) and its text."""
    language: str
    origin: str
    text: str


def _interpreter_language(word: str) -> str:
    base = word.rsplit('/', 1)[-1]
    if _PYTHON_WORD_RE.fullmatch(base):
        return 'python'
    if base in _OTHER_INTERPRETERS:
        return 'other'
    return 'shell' if base in _SHELL_WORDS else ''


def _shebang_language(text: str) -> str:
    first = text.split('\n', 1)[0]
    if not first.startswith('#!'):
        return ''
    words = [w for w in first[2:].split() if w != '-S']
    if words and words[0].rsplit('/', 1)[-1] == 'env':
        words = words[1:]
    return _interpreter_language(words[0]) if words else ''


def _script_file(words: list[str]) -> tuple[str, str]:
    """The script file a simple command runs and its language as far as the
    command says ('' when only the file can tell): the interpreter's first
    operand, or the command word itself when it is a path (`./build.sh`)."""
    language = _interpreter_language(words[0])
    if not language:
        return (words[0], '') if '/' in words[0] else ('', '')
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
        elif word in _NO_SCRIPT_FILE_OPTS or (language == 'shell' and word == '-n'):
            return '', ''  # inline source, stdin, a module, or a syntax check only
        elif word in _INTERPRETER_VALUE_OPTS:
            skip = True
        elif not word.startswith('-'):
            return word, language
    return '', ''


def _read_script(path: str, root: str) -> str | None:
    full = os.path.join(root, os.path.expanduser(path))
    try:
        if not os.path.isfile(full) or os.path.getsize(full) > _SCRIPT_MAX_BYTES:
            return None
        with open(full, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _tracked_by_git(path: str) -> bool:
    try:
        done = subprocess.run(('git', '-C', os.path.dirname(path) or '.', 'ls-files',
                               '--error-unmatch', '--', os.path.basename(path)),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# The playbook's own scripts that its skills tell the model to run, as
# `python3 <playbook>/<path>`. Installed by `install.sh` (a release tarball) or
# `claude plugin install` (the plugin cache), none of them is in a git work
# tree, so the tracked-file exemption below never applied: the destructive gate
# read lab/run.py, found the `shutil.rmtree` of its own temporary directory,
# and denied every lab step outside this repository. A fixed list, not every
# file under the root: a script written into the plugin directory and run from
# there is still read.
PLAYBOOK_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PLAYBOOK_ENTRY_SCRIPTS = ('lab/run.py', 'scripts/playbook_config.py', 'hooks/output_filter.py',
                          'audit/surface.py', 'scripts/temporary_data.py')


def _is_playbook_entry_script(path: str) -> bool:
    """Whether `path`, links resolved, is one of PLAYBOOK_ENTRY_SCRIPTS. A link
    inside the plugin that points elsewhere resolves to where it points."""
    real = os.path.realpath(path)
    return any(real == os.path.join(PLAYBOOK_ROOT, rel) for rel in PLAYBOOK_ENTRY_SCRIPTS)


def script_sources(raw: str, cwd: str, untracked_only: bool = False) -> list[ScriptSource]:
    """Scripts `raw` hands an interpreter other than inline: a heredoc body fed
    to python/node/ruby/perl/php, and a script file named as an interpreter's
    operand or run by its path. A file that does not exist (yet), is past
    1 MB, or whose language neither the command, its shebang nor its extension
    names is not read. A shell's heredoc is not returned: it is already part
    of the command text the gates read.

    `untracked_only` leaves out script files git tracks: the project's own
    build and test scripts (`./mvnw`, `tests/run_all.py`) clean up after
    themselves, and reading them blocked 184 of 6885 recorded commands. A
    script the agent writes and then runs is untracked, and that is the way
    past the gate this closes. The playbook's own entry scripts are left out
    the same way, wherever it is installed (`_is_playbook_entry_script`)."""
    sources: list[ScriptSource] = []
    for start in _HEREDOC_START_RE.finditer(raw):
        language = _interpreter_language(_segment_command_word(raw, start.start()))
        if language not in ('python', 'other'):
            continue
        body = _heredoc_body_re(start).match(raw, start.start())
        if body:
            sources.append(ScriptSource(language, 'heredoc', body.group(2)))
    root = _command_root(raw, cwd) or cwd or os.getcwd()
    runnable = strip_inline_payloads(strip_heredoc_bodies(raw))
    for words in _read_commands(runnable)[0]:
        path, language = _script_file(words)
        if not path or not _is_literal_path(path):
            continue
        full = os.path.join(root, os.path.expanduser(path))
        if untracked_only and (_is_playbook_entry_script(full) or _tracked_by_git(full)):
            continue
        text = _read_script(path, root)
        if text is None:
            continue
        language = (language or _shebang_language(text)
                    or _SCRIPT_EXT_LANGUAGE.get(os.path.splitext(path)[1].lower(), ''))
        if language:
            sources.append(ScriptSource(language, path, text))
    return sources


def _python_tree(text: str) -> ast.AST | None:
    """The syntax tree of a Python script; None when it does not parse, in
    which case Python would not run it either."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # a stray escape is not ours to report
            return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ''


def _literal_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _python_bound_paths(tree: ast.AST) -> dict[str, str]:
    """Names a script binds once to a literal path (`p = 'a.ts'`,
    `p = Path('a.ts')`); a name bound to anything else, or twice, is left
    out because its value is not known."""
    bound: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        name, value = node.targets[0].id, node.value
        if (isinstance(value, ast.Call) and _call_name(value.func) in ('Path', 'PurePath')
                and len(value.args) == 1):
            value = value.args[0]
        literal = _literal_str(value)
        bound[name] = literal if name not in bound and literal else None
    return {name: path for name, path in bound.items() if path}


def _python_path_of(node: ast.expr | None, bound: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    if (isinstance(node, ast.Call) and _call_name(node.func) in ('Path', 'PurePath')
            and len(node.args) == 1):
        return _python_path_of(node.args[0], bound)
    return _literal_str(node) if node is not None else None


def _python_write_paths(tree: ast.AST) -> tuple[list[str], bool]:
    """Paths a Python script opens for writing (`open(p, 'w')`,
    `Path(p).write_text(...)`, `p.write_text(...)` with `p` bound once to a
    literal), and whether it also writes where the script does not say
    (`open(path, 'w')` with `path` a parameter): the read-replace-write edit
    helper, which a heredoc's caller resolves from the paths it names."""
    bound = _python_bound_paths(tree)
    paths: list[str] = []
    unresolved = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        owner = node.func.value if isinstance(node.func, ast.Attribute) else None
        mode_kw = next((k.value for k in node.keywords if k.arg == 'mode'), None)
        if name in ('write_text', 'write_bytes') and owner is not None:
            path, mode_node = _python_path_of(owner, bound), ast.Constant('w')
        elif name == 'open' and owner is not None and not isinstance(owner, ast.Attribute):
            # `Path(..).open(mode)` / `p.open(mode)`; `io.open` is the builtin below.
            if isinstance(owner, ast.Name) and owner.id in ('io', 'codecs'):
                path = _python_path_of(node.args[0], bound) if node.args else None
                mode_node = node.args[1] if len(node.args) > 1 else mode_kw
            else:
                path = _python_path_of(owner, bound)
                mode_node = node.args[0] if node.args else mode_kw
        elif name == 'open' and owner is None and node.args:
            path = _python_path_of(node.args[0], bound)
            mode_node = node.args[1] if len(node.args) > 1 else mode_kw
        else:
            continue
        mode = _literal_str(mode_node) if mode_node is not None else 'r'
        writes = mode is None or bool(set(mode) & set('wax+'))  # a computed mode may write
        if writes and path:
            paths.append(path)
        elif writes:
            unresolved = True
    return paths, unresolved


def _python_source_paths(tree: ast.AST) -> list[str]:
    """String constants of a script that name a source file."""
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and _CODE_PATH_WORD_RE.fullmatch(node.value)]


# A name bound once to a directory the script itself creates as temporary:
# `const dir = fs.mkdtempSync(...)`, `let d = await mkdtemp(...)`.
_TEXT_TEMP_DIR_RE = re.compile(
    r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?(?:[\w$]+\s*\.\s*)*'
    r'mkdtemp(?:Sync)?\s*\(')
_FIRST_ARGUMENT_NAME_RE = re.compile(r'\(\s*([A-Za-z_$][\w$]*)\s*[,)]')


def _text_temp_dirs(text: str) -> set[str]:
    """Names a script binds to a temporary directory it creates and never
    rebinds: deleting one removes only what the script made. A name that is
    also assigned again, a parameter (`(dir) =>`, `function f(dir)`), a loop
    variable or a `catch` binding is left out: the text does not show scope."""
    names = set()
    for name in _TEXT_TEMP_DIR_RE.findall(text):
        word = re.escape(name)
        rebound = (len(re.findall(rf'(?<![\w$.]){word}\s*=(?![=>])', text)) != 1
                   or re.search(rf'\([^()]*(?<![\w$.]){word}(?![\w$])[^()]*\)\s*(?:=>|\{{)', text)
                   or re.search(rf'(?<![\w$.]){word}\s*=>', text)
                   or re.search(rf'\bfor\s*\([^)]*(?<![\w$.]){word}(?![\w$])', text)
                   or re.search(rf'\bcatch\s*\(\s*{word}\b', text))
        if not rebound:
            names.add(name)
    return names


def _recursive_delete_in_text(text: str) -> str:
    """The first recursive-deletion call of a script read as text, other than
    one on a temporary directory the script created; '' when there is none."""
    temp_dirs = _text_temp_dirs(text)
    for match in _RECURSIVE_DELETE_API_RE.finditer(text):
        argument = _FIRST_ARGUMENT_NAME_RE.search(text, match.start(), match.end() + 120)
        if argument and argument.start() < match.end() and argument.group(1) in temp_dirs:
            continue
        return match.group(0).strip()
    return ''


def _python_temp_dirs(tree: ast.AST) -> set[str]:
    """Names a Python script binds once, and only, to `tempfile.mkdtemp()`:
    any other binding of the name (a loop target, `with ... as`, `:=`, a second
    assignment, a parameter) leaves it out."""
    stores: dict[str, int] = {}
    parameters: set[str] = set()
    temp: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores[node.id] = stores.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):
            parameters.add(node.arg)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)
              and _call_name(node.value.func) == 'mkdtemp'):
            temp.add(node.targets[0].id)
    return {name for name in temp if stores.get(name) == 1 and name not in parameters}


def _recursive_delete_call(source: ScriptSource) -> str:
    """The recursive-deletion call a python/other script makes, as text; ''
    when it makes none. A call on a temporary directory the script itself
    created (`mkdtemp`) deletes only what the script made, and is not one."""
    if source.language != 'python':
        return _recursive_delete_in_text(source.text)
    tree = _python_tree(source.text)
    temp_dirs = _python_temp_dirs(tree) if tree is not None else set()
    for node in ast.walk(tree) if tree is not None else ():
        if isinstance(node, ast.Call) and _call_name(node.func) in _PY_RECURSIVE_DELETE_CALLS:
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Name) and first.id in temp_dirs:
                continue
            return (ast.get_source_segment(source.text, node) or _call_name(node.func))[:120]
    return ''


def _shell_script_text(text: str) -> str:
    """A shell script without its comment lines: `# rm -rf build` documents
    a step, it does not run one."""
    return '\n'.join(line for line in text.split('\n') if not line.lstrip().startswith('#'))


# Calls through which a script hands a command line to the system.
_PY_SHELL_CALLS = frozenset({'system', 'popen', 'run', 'call', 'check_call', 'check_output',
                             'Popen', 'getoutput', 'getstatusoutput'})
_TEXT_SHELL_CALL_RE = re.compile(
    r"""\b(?:system|exec|execSync|spawnSync|spawn|popen|shell_exec|passthru)\s*\(\s*"""
    r"""(['"`])((?:\\.|(?!\1).)*)\1""")


def _python_shell_commands(text: str) -> list[str]:
    """Command lines a Python script runs through os.system/subprocess: a
    string argument as written, a list of strings joined."""
    tree = _python_tree(text)
    commands: list[str] = []
    for node in ast.walk(tree) if tree is not None else ():
        if not (isinstance(node, ast.Call) and _call_name(node.func) in _PY_SHELL_CALLS
                and node.args):
            continue
        first = node.args[0]
        if _literal_str(first) is not None:
            commands.append(_literal_str(first) or '')
        elif isinstance(first, (ast.List, ast.Tuple)):
            parts = [_literal_str(e) for e in first.elts]
            if parts and all(p is not None for p in parts):
                commands.append(' '.join(shlex.quote(p or '') for p in parts))
    return commands


def _unquote_payload(payload: str) -> str:
    try:
        words = shlex.split(payload)
    except ValueError:
        return payload.strip('$\'"')
    return words[0] if len(words) == 1 else payload


def interpreter_shell_commands(raw: str, cwd: str) -> list[str]:
    """Shell command lines that interpreter scripts in `raw` run: from
    `-c`/`-e` one-liners, interpreter heredocs and untracked script files
    (`python3 -c "import os; os.system('nmap host')"`). The gates read these
    as commands: stripped as foreign source, a scanner or an `rm -rf` run
    from a one-liner went unread."""
    commands: list[str] = []
    for match in _INLINE_PAYLOAD_RE.finditer(strip_heredoc_bodies(raw)):
        language = _interpreter_language(match.group(1).split()[0])
        source = _unquote_payload(match.group(0)[len(match.group(1)):])
        commands += (_python_shell_commands(source) if language == 'python'
                     else [m.group(2) for m in _TEXT_SHELL_CALL_RE.finditer(source)])
    for script in script_sources(raw, cwd, untracked_only=True):
        if script.language == 'python':
            commands += _python_shell_commands(script.text)
        elif script.language == 'other':
            commands += [m.group(2) for m in _TEXT_SHELL_CALL_RE.finditer(script.text)]
    return [c for c in commands if c.strip()]

# ─── Bash write targets beyond redirects ─────────────────────────────────────

_COPY_WORDS = frozenset({'cp', 'mv', 'install', 'ln'})
_COPY_VALUE_OPTS = frozenset({'-m', '--mode', '-o', '--owner', '-g', '--group', '-S',
                              '--suffix', '-t', '--target-directory'})
_PATCH_READ_ONLY = frozenset({'--check', '--stat', '--numstat', '--summary', '--dry-run'})
# A file-writing call in an interpreter one-liner (open for write/append,
# write_text, writeFileSync, File.write...).
_INTERPRETER_WRITE_API_RE = re.compile(
    r"""\bopen\s*\([^)]*['"][wax]\+?b?['"]|\.write_(?:text|bytes)\s*\("""
    r'|\b(?:writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream)\s*\('
    r'|\bFile\s*\.\s*(?:write|open\s*\([^)]*[\'"]w)')
_CODE_PATH_WORD_RE = re.compile(r'[^\s\'"]+' + _CODE_EXT, re.IGNORECASE)
_QUOTED_CODE_PATH_RE = re.compile(r"""['"]([^'"\s]+{e})['"]""".format(e=_CODE_EXT))


def _code_paths(words: list[str]) -> list[str]:
    return [w for w in words if not w.startswith('-') and CODE_EXT_RE.search(w)]


def _names_a_file(path: str) -> bool:
    """Whether `path` names one file the gate can judge by its extension.
    A directory (`.`, `src/`, `src`), a glob or a variable can hold source,
    so it cannot be cleared by name."""
    name = os.path.basename(path)
    return (bool(name) and '.' in name.lstrip('.') and not path.endswith('/')
            and _is_literal_path(path))


def _is_literal_path(word: str) -> bool:
    """Whether `word` is a path as written, not one the shell or another
    program fills in (`$f`, a substitution, a glob, find's `{}`)."""
    return not any(ch in word for ch in '$`*?[{')


def _copy_destinations(words: list[str]) -> list[str]:
    """Where `cp`/`mv`/`install`/`ln` put source files."""
    positional: list[str] = []
    target_dir = ''
    i = 1
    while i < len(words):
        word = words[i]
        if word in ('-t', '--target-directory') and i + 1 < len(words):
            target_dir = words[i + 1]
            i += 2
            continue
        if word.startswith('--target-directory='):
            target_dir = word.split('=', 1)[1]
        elif word in _COPY_VALUE_OPTS:
            i += 1
        elif not word.startswith('-'):
            positional.append(word)
        i += 1
    if target_dir:
        return [os.path.join(target_dir, os.path.basename(s)) for s in _code_paths(positional)]
    if len(positional) < 2:
        return []
    dest, sources = positional[-1], positional[:-1]
    if CODE_EXT_RE.search(dest):
        return [dest]
    return [os.path.join(dest, os.path.basename(s)) for s in _code_paths(sources)]


def _download_destinations(base: str, words: list[str]) -> list[str]:
    """Where `curl -o`/`-O` and `wget [-O]` save what they fetch."""
    out: list[str] = []
    for i, word in enumerate(words[1:], start=1):
        nxt = words[i + 1] if i + 1 < len(words) else ''
        if (base == 'curl' and word in ('-o', '--output')) or (base == 'wget' and word == '-O'):
            out.append(nxt)
        elif word.startswith(('--output=', '--output-document=')):
            out.append(word.split('=', 1)[1])
    # wget without -O, and curl -O, name the file after the URL.
    named_by_url = ((base == 'wget' and not out)
                    or (base == 'curl' and any(
                        w == '--remote-name' or re.fullmatch(r'-[a-zA-Z]*O[a-zA-Z]*', w)
                        for w in words)))
    if named_by_url:
        out += [os.path.basename(w.split('?', 1)[0]) for w in words if '://' in w]
    return [p for p in out if p and CODE_EXT_RE.search(p)]


def _nested_scripts(words: list[str]) -> list[str]:
    """Scripts a simple command hands to a shell: `eval`'s arguments, and the
    `-c` script of every shell word in it, wherever it sits — a shell run as
    an argument (`find -exec sh -c '...'`, `docker exec ctr sh -c '...'`)
    runs its script as surely as one that is the command word."""
    if words[0].rsplit('/', 1)[-1] == 'eval':
        return [' '.join(words[1:])]
    scripts: list[str] = []
    for at, word in enumerate(words):
        if word.rsplit('/', 1)[-1] not in _SHELL_WORDS:
            continue
        script = next((words[j + 1] for j in range(at + 1, len(words) - 1)
                       if re.fullmatch(r'-[a-z]*c[a-z]*', words[j])), None)
        if script is not None:
            scripts.append(script)
    return scripts


def _raw_simple_commands(command: str
                         ) -> tuple[list[tuple[list[str], bool]], list[str]] | None:
    """Every simple command of `command` as written, wrappers kept (`xargs rm`
    is a mass delete because of its wrapper), with the scripts it hands to a
    shell (see `_nested_scripts`) read as commands of their own and flagged
    as nested; plus those nested scripts that do not tokenize, as text.

    A nested script that does not tokenize is returned for a text check and
    leaves the commands around it standing: dropping the whole command for it
    let `cp x.py src/ && bash -c "echo it's done"` hide the `cp`. None only
    when `command` itself does not tokenize."""
    segments = shell_segments(command)
    if segments is None:
        return None
    found: list[tuple[list[str], bool]] = []
    opaque: list[str] = []
    for segment in segments:
        found.append((segment, False))
        words = command_words(segment)
        for script in _nested_scripts(words) if words else ():
            nested = _raw_simple_commands(script)
            if nested is None:
                opaque.append(script)
            else:
                found += [(inner, True) for inner, _ in nested[0]]
                opaque += nested[1]
    return found, opaque


def _simple_commands(command: str) -> tuple[list[list[str]], list[str]] | None:
    """`_raw_simple_commands` with each command reduced to its command words
    (wrappers and assignments dropped), and assignment-only ones left out."""
    read = _raw_simple_commands(command)
    if read is None:
        return None
    commands = [words for words in (command_words(segment) for segment, _ in read[0]) if words]
    return commands, read[1]


def _read_commands(command: str) -> tuple[list[list[str]], list[str]]:
    """`_simple_commands`, with a command that does not tokenize returned
    whole as the one text to check."""
    read = _simple_commands(command)
    return read if read is not None else ([], [command])


def _sed_in_place_operands(words: list[str]) -> list[str] | None:
    """File operands of a `sed` that edits in place, wherever it sits in the
    command (`find -exec sed -i ...` runs it as an argument); None when no
    `sed` in `words` edits in place.

    Options are parsed the way GNU sed reads them (anywhere before `--`), so
    `-i` is caught inside a cluster (`-ni`, `-Ei.bak`) and as `--in-place`, and
    the values of `-e`/`-f`/`-l` are not taken for files. Without `-e`/`-f` the
    first operand is the script, not a file."""
    sed_at = next((j for j, w in enumerate(words) if w.rsplit('/', 1)[-1] == 'sed'), None)
    if sed_at is None:
        return None
    args = words[sed_at + 1:]
    in_place = has_script_option = False
    operands: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        i += 1
        if word == '--':
            operands += args[i:]
            break
        if word.startswith('--'):
            name = word[2:].split('=', 1)[0]
            if name == 'in-place':
                in_place = True
            elif name in ('expression', 'file'):
                has_script_option = True
                i += 0 if '=' in word else 1
            elif name == 'line-length' and '=' not in word:
                i += 1
            continue
        if word.startswith('-') and len(word) > 1:
            for at, flag in enumerate(word[1:], start=1):
                if flag == 'i':
                    in_place = True
                    break  # the rest of the cluster is the backup suffix
                if flag in 'efl':
                    has_script_option = has_script_option or flag in 'ef'
                    i += 0 if at < len(word) - 1 else 1  # value attached, else next word
                    break
            continue
        operands.append(word)
    if not in_place:
        return None
    return operands if has_script_option else operands[1:]


def _runs_sed_in_place(command: str) -> bool:
    """Whether `command` edits any file in place with `sed`, whatever its
    extension; the text pattern decides for what does not tokenize, and inside
    an interpreter one-liner (`python3 -c "os.system('sed -i ...')"`)."""
    commands, opaque = _read_commands(command)
    return (any(_sed_in_place_operands(words) is not None for words in commands)
            or any(_SED_IN_PLACE_RE.search(text) for text in opaque + inline_payloads(command)))


def bash_write_targets(command: str) -> tuple[list[str], bool]:
    """Source files a command writes through anything but a redirect or `tee`
    (those are BASH_CODE_WRITE_TARGET_RE's), as written in the command.

    The second value is True when the command writes source to places it does
    not name — `git apply`, `patch`, `git am`, `git checkout <ref> -- <dir>` —
    which the gates treat as a code write, as they do any unresolvable target.
    Text that does not tokenize (the command, or a nested `sh -c` script) falls
    back to the text pattern for `sed -i` and counts as unresolvable when it
    matches (fail closed). So does a `sed -i` whose files are filled in at run
    time (`$(git ls-files '*.py')`, find's `{}`, `xargs`) when the command
    names a source extension anywhere (`find src -name '*.ts' -exec sed -i
    ... {} +`); the same edit over `*.md` stays clear."""
    targets: list[str] = []
    commands, opaque = _read_commands(command)
    unresolvable = any(_SED_IN_PLACE_TEXT_RE.search(text) for text in opaque)
    for words in commands:
        base = words[0].rsplit('/', 1)[-1]
        sed_files = _sed_in_place_operands(words)
        if sed_files is not None:
            targets += _code_paths(sed_files)
            if not sed_files or not all(map(_is_literal_path, sed_files)):
                unresolvable = unresolvable or bool(CODE_EXT_RE.search(command))
        if base in _COPY_WORDS:
            targets += _copy_destinations(words)
        elif base == 'dd':
            targets += [w[3:] for w in words if w.startswith('of=') and CODE_EXT_RE.search(w)]
        elif base in ('curl', 'wget'):
            targets += _download_destinations(base, words)
        elif base == 'perl' and any(re.fullmatch(r'-\w*i\S*', w) for w in words[1:]):
            script = {words[i + 1] for i, w in enumerate(words[:-1]) if w in ('-e', '-E')}
            targets += [p for p in _code_paths(words[1:]) if p not in script]
        elif base in ('awk', 'gawk') and 'inplace' in words:
            targets += _code_paths(words[1:])
        elif base == 'patch' and not _PATCH_READ_ONLY & set(words):
            unresolvable = True
        elif base == 'git':
            sub = _GIT_GLOBAL_OPTS_RE.sub('git ', ' '.join(words)).split()[1:]
            if not sub:
                continue
            if sub[0] == 'apply' and not _PATCH_READ_ONLY & set(sub):
                unresolvable = True
            elif sub[0] == 'am':
                unresolvable = True
            elif sub[0] == 'restore' and {'--staged', '-S'} & set(sub) and not (
                    {'--worktree', '-W'} & set(sub)):
                continue  # only unstages: the working tree is untouched
            elif sub[0] in ('checkout', 'restore') and (sub[0] == 'restore' or '--' in sub):
                paths = sub[sub.index('--') + 1:] if '--' in sub else [
                    w for w in sub[1:] if not w.startswith('-')]
                targets += _code_paths(paths)
                unresolvable = unresolvable or any(not _names_a_file(p) for p in paths)
    for payload in inline_payloads(command):
        if _INTERPRETER_WRITE_API_RE.search(payload):
            targets += _QUOTED_CODE_PATH_RE.findall(payload)
        # A shell command run from the one-liner (`os.system("sed -i ...")`).
        unresolvable = unresolvable or bool(_SED_IN_PLACE_TEXT_RE.search(payload))
    return targets, unresolvable


def _interpreter_operand_paths(raw: str) -> list[str]:
    """Source paths passed as arguments to a Python interpreter in `raw`."""
    runnable = strip_inline_payloads(strip_heredoc_bodies(raw))
    return [path for words in _read_commands(runnable)[0]
            if _interpreter_language(words[0]) == 'python'
            for path in _code_paths(words[1:])]


def script_write_targets(raw: str, cwd: str, depth: int = 0) -> tuple[list[str], bool]:
    """Source files the scripts `raw` runs (see `script_sources`) write, as
    written, and whether one writes source where it does not name.

    A Python script is read by its syntax tree, another language for a write
    call with a literal path, and a shell script as a command of its own
    (redirects, `bash_write_targets`, and the scripts it runs in turn)."""
    targets: list[str] = []
    unresolvable = False
    for source in script_sources(raw, cwd):
        if source.language == 'python':
            tree = _python_tree(source.text)
            if tree is None:
                continue
            paths, unresolved_write = _python_write_paths(tree)
            targets += [p for p in paths if CODE_EXT_RE.search(p)]
            if unresolved_write and source.origin == 'heredoc':
                # A heredoc is written for this one command: where it writes
                # is a parameter, so the source files it names, and those the
                # command hands it (`python3 - a.ts`), are what it edits. A
                # script file's string constants say nothing of the kind.
                targets += _python_source_paths(tree) + _interpreter_operand_paths(raw)
        elif source.language == 'other':
            targets += [p for p in _TEXT_WRITE_CALL_RE.findall(source.text) if CODE_EXT_RE.search(p)]
        elif depth < _SCRIPT_MAX_DEPTH:
            text = _shell_script_text(source.text)
            stripped = strip_heredoc_bodies(text)
            targets += _redirect_or_text_targets(stripped)
            for found, found_unresolvable in (bash_write_targets(stripped),
                                              script_write_targets(text, cwd, depth + 1)):
                targets += found
                unresolvable = unresolvable or found_unresolvable
    return [t for t in targets if t], unresolvable


# ─── Local validation lab ────────────────────────────────────────────────────
#
# Scanners run only through lab/run.py, which confines them to an internal
# Docker network shared with the project's own application. The script calls
# Docker as a subprocess, so the hook never sees its commands; everything the
# agent runs itself is checked here. There is no override: anything outside
# the lab — published, staging, someone else's — is never a target.

SCANNER_WORDS = frozenset({
    'zap.sh', 'zap-baseline.py', 'zap-full-scan.py', 'zap-api-scan.py', 'zaproxy',
    'nuclei', 'sqlmap', 'nikto', 'nmap', 'masscan', 'zmap', 'ffuf', 'gobuster', 'dirb',
    'dirsearch', 'feroxbuster', 'wfuzz', 'hydra', 'medusa', 'wpscan', 'strix',
})
# A scanner image is a whole path segment of an image reference, before its tag
# or digest (`ghcr.io/zaproxy/zaproxy:stable`, `instrumentisto/nmap`): read as
# a substring, `nmap` matched `test_unmapped_fields.py` and `unmap_users`.
_SCANNER_IMAGE_RE = re.compile(
    r'(?:^|/)(?:zaproxy|zap(?:2docker)?(?:-[\w.]+)?|nuclei|sqlmap|nikto|nmap|masscan|ffuf'
    r'|gobuster|feroxbuster|wpscan)(?:[:@]|$)|(?:^|/)usestrix/|\bredlab\b', re.IGNORECASE)
# Container subcommands that do not run anything: inspecting, downloading,
# listing, removing, and tearing a leftover lab down are all fine.
_CONTAINER_READ_ONLY = frozenset({'pull', 'rm', 'rmi', 'inspect', 'images', 'ls', 'ps',
                                  'logs', 'down', 'stop', 'kill', 'version', 'info'})
_CONTAINER_TOOLS = frozenset({'docker', 'podman', 'nerdctl'})

_LAB_MSG = (
    '[playbook:lab] Scanners run only inside the local lab: `python3 <playbook>/lab/run.py '
    '--service <compose service> --port <port>`. The lab runs an isolated copy of the '
    "project's own stack on an internal Docker network, so nothing outside it can be "
    'reached. Run `lab/run.py --setup` first if the scanner image is missing. Scanning '
    'anything else (a published, staging or third-party host) is not done here.'
)


_SCANNER_WORD_RE = re.compile(
    r'(?<![\w.-])(?:' + '|'.join(map(re.escape, sorted(SCANNER_WORDS))) + r')(?![\w.-])')


def check_lab_scanner(command: str, raw: str = '', cwd: str = '', _depth: int = 0) -> dict | None:
    """Deny running a scanner, or a scanner container, outside lab/run.py.

    `command` is the runnable command; `raw`, the full one, is read for the
    command lines interpreter scripts run (`interpreter_shell_commands`). A
    scanner inside a shell's `-c` script or `eval` is read like any other,
    and a nested script that does not tokenize is read as text. A scanner
    renamed or hidden behind a tracked script file gets past this, as any text
    check can be; the lab's internal network is the actual boundary, and it
    only exists inside the lab."""
    commands, opaque = _read_commands(command)
    if any(_SCANNER_WORD_RE.search(text) for text in opaque):
        return {'block': True, 'msg': _LAB_MSG}
    if raw and _depth == 0:
        for line in interpreter_shell_commands(raw, cwd):
            if check_lab_scanner(line, _depth=1):
                return {'block': True, 'msg': _LAB_MSG}
    for words in commands:
        base = words[0].rsplit('/', 1)[-1]
        if base in SCANNER_WORDS:
            return {'block': True, 'msg': _LAB_MSG}
        if base in _CONTAINER_TOOLS:
            if _container_subcommand(words) in _CONTAINER_READ_ONLY:
                continue
            if any(_SCANNER_IMAGE_RE.search(w) for w in words[1:]):
                return {'block': True, 'msg': _LAB_MSG}
    return None


# Global options of docker / docker compose that take the next word as value.
_CONTAINER_VALUE_OPTS = frozenset({
    '-p', '--project-name', '-f', '--file', '--profile', '--env-file', '--project-directory',
    '-H', '--host', '--context', '-c', '--config', '-l', '--log-level', '--ansi',
    '--parallel', '--progress',
})


def _container_subcommand(words: list[str]) -> str:
    """The verb of a docker/podman command: `rm` in `docker image rm x`,
    `down` in `docker compose -p p -f f.json down`. Group words (`compose`,
    `container`, `image`) are stepped over; their options are skipped with
    their values."""
    i = 1
    while i < len(words):
        word = words[i]
        if word.startswith('-'):
            i += 2 if word in _CONTAINER_VALUE_OPTS else 1
            continue
        if word in ('compose', 'container', 'image', 'network', 'volume'):
            i += 1
            continue
        return word
    return ''


# Commands that count as validation for the Stop-hook check.
VALIDATION_RE = re.compile(
    r'\b(?:tsc|vitest|jest|biome|eslint)\b'
    # A workspace filter before the script is still the script (`pnpm -F web
    # test`, `npm --workspace=api run lint`); a monorepo runs nothing else.
    r'|\b(?:npm|pnpm|yarn|bun)\s+(?:(?:-F|--filter|-w|--workspace|-r|--recursive)(?:[=\s]+[^\s-]\S*)?\s+)*'
    r'(?:run\s+)?(?:test|lint|typecheck|type-check|check|build|format:check)\b'
    r'|\bturbo\s+(?:run\s+)?\S*(?:test|lint|typecheck|type-check|check|build)\b'
    r'|\bprettier\s+(?:\S+\s+)*(?:--check|-c)\b|\b(?:next|expo)\s+(?:lint|build)\b'
    r'|\bpytest\b|\bpython3?\s+\S*(?:test|run_all)\S*'
    # Language opt-ins: each must have a way to clear pending_validation, or the
    # Stop gate blocks a run that did validate.
    r'|\bgo\s+(?:test|build|vet)\b|\bgolangci-lint\b'
    r'|\bcargo\s+(?:test|build|check|clippy|fmt|nextest)\b'
    r'|\b(?:mvn|gradle|gradlew)\b|\bktlint\b|\bdetekt\b'
    r'|\bdotnet\s+(?:test|build|format)\b'
    r'|\brspec\b|\brubocop\b|\brake\s+(?:test|spec)\b'
    r'|\b(?:ruff|mypy|pyright|tox|nox|flake8|black)\b'
    r'|\bpython3?\s+-m\s+(?:pytest|unittest|mypy|ruff|py_compile|compileall)\b'
    # Syntax-only checks: the least an interpreter can assert about a file it was
    # just handed. Weaker than a type check or a test run, but real validation --
    # and on a machine with no linter installed it is the ONLY thing the toolchain
    # offers, so without these the gate has no clearable command at all and fires
    # on every turn that writes. Not a lower bar than what is already here: this
    # list admits pure formatters (gofmt, cargo fmt, black, shfmt), which assert
    # no more about a file than a syntax check does.
    r'|(?<![.\w-])(?:ba|z|k|da|a)?sh\s+(?:-\S+\s+)*-n\b'
    r'|\bnode\s+--check\b|\bruby\s+-c\b|\bphp\s+-l\b|\bperl\s+-c\b'
    r'|\b(?:make|just|bazel|ninja|ctest|cmake)\s+\S*(?:test|check|lint|build|verify|ci)\S*\b'
    r'|\bmake\s*$'
    r'|\b(?:phpunit|pest)\b|\bcomposer\s+(?:test|lint|check)\b'
    r'|\bmix\s+(?:test|compile|credo|format)\b'
    r'|\bswift\s+(?:test|build)\b'
    r'|\b(?:deno|dart|flutter)\s+(?:test|check|lint|fmt|analyze|task)\b'
    r'|\bsbt\s+(?:test|compile)\b'
    r'|\bzig\s+(?:build|test)\b'
    r'|\b(?:gofmt|gofumpt|staticcheck|shellcheck|shfmt)\b'
    # Static analysis, when installed (engineering-security, "Static analysis").
    r'|\b(?:semgrep|opengrep)\s+(?:scan|ci)\b'
    r'|\bstack\s+(?:test|build)\b|\bcabal\s+(?:test|build)\b',
    re.IGNORECASE,
)


def is_validation_command(command: str) -> bool:
    """A Bash command that validates, read with heredoc bodies and interpreter
    payloads stripped: a doc that merely mentions `npm test` validates nothing."""
    return bool(VALIDATION_RE.search(strip_inline_payloads(strip_heredoc_bodies(command))))

# Static analysis engines the playbook delegates to when one is installed. Never
# bundled: the engine is LGPL and the community rules may not be redistributed,
# so the playbook only tells the model how to run what the user already has.
SAST_ENGINES = ('semgrep', 'opengrep')


def installed_sast() -> str:
    """The first SAST engine on PATH, or ''."""
    import shutil
    return next((e for e in SAST_ENGINES if shutil.which(e)), '')


def sast_notice(cwd: str) -> str:
    """Session context naming the installed engine and the exact command, ''
    when none is installed. Detection happens once per session, here, and not
    in the Stop gate: a scan takes seconds and the Stop hook has ten, and a
    hook that times out lets everything through."""
    engine = installed_sast()
    if not engine:
        return ''
    local = next((c for c in ('.semgrep.yml', '.semgrep.yaml', '.semgrep')
                  if os.path.exists(os.path.join(cwd or os.getcwd(), c))), '')
    if local:
        config = local
    elif engine == 'semgrep':
        config = 'p/default'
    else:
        return (f'[playbook:sast] `{engine}` is installed but this project has no local rule '
                'config (`.semgrep.yml` or `.semgrep/`), so no scan is expected. See '
                '"Static analysis" in engineering-security.')
    metrics = ' --metrics=off' if engine == 'semgrep' else ''
    return (
        f'[playbook:sast] `{engine}` is installed. After writing source, include in the '
        f'validation: `{engine} scan{metrics} --error --severity ERROR --config {config} '
        '<changed files>`. Handle findings as review candidates, never add `nosemgrep` '
        'yourself, and if the scan cannot run (network, timeout), say so and go on. '
        'Rules: "Static analysis" in engineering-security.'
    )


def _language_markers() -> list[tuple[str, list[str]]]:
    """(language, marker files) per row of the skill requirements table, in its
    order: the same rows the write gate maps extensions with, so a language the
    gate knows is also one the session can name."""
    languages = skill_requirements().get('languages')
    if not isinstance(languages, dict):
        return []
    return [(name, [str(m) for m in row.get('markers', ())])
            for name, row in languages.items() if isinstance(row, dict)]


def detect_languages(cwd: str) -> list[tuple[str, str]]:
    """(language, the marker that proved it) for every playbook language whose
    marker file sits in the project root. Shallow by design: a marker deeper in
    the tree is usually a vendored or example sub-project, not the stack. A
    repository with no known marker returns []."""
    root = cwd or os.getcwd()
    found: list[tuple[str, str]] = []
    for lang, markers in _language_markers():
        for marker in markers:
            if '*' in marker:
                hit = next((os.path.basename(p)
                            for p in sorted(glob.glob(os.path.join(root, marker)))), '')
            else:
                hit = marker if os.path.exists(os.path.join(root, marker)) else ''
            if hit:
                found.append((lang, hit))
                break
    return found


def language_notice(cwd: str) -> str:
    """Session context naming the implementation language found in the project
    root and the engineering-<lang> opt-in to load, '' when none is found. A
    hint for the routing table, not an override: the model still confirms the
    language, and a project without a marker stays silent."""
    found = detect_languages(cwd)
    if not found:
        return ''
    if len(found) == 1:
        lang, marker = found[0]
        return (f'[playbook:language] Detected {lang} ({marker}). Load the '
                f'engineering-{lang} opt-in before writing or reviewing code.')
    listed = ', '.join(f'{lang} ({marker})' for lang, marker in found)
    opts = ', '.join(f'engineering-{lang}' for lang, _ in found)
    return (f'[playbook:language] Multiple languages in the root: {listed}. Load '
            f'the matching opt-in ({opts}) for the part you touch.')


# ─── Skills a write requires ─────────────────────────────────────────────────

_SKILL_REQUIREMENTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data', 'skill_requirements.json')
_skill_requirements_cache: dict | None = None
_SOURCE_READ_LIMIT = 512 * 1024
_MANIFEST_WALK_LIMIT = 16
_PACKAGE_JSON_SECTIONS = ('dependencies', 'devDependencies', 'peerDependencies',
                          'optionalDependencies')

# Only lines shaped as an import are read (and a dynamic import only with a
# literal quoted argument), so a comment or docstring that mentions a package
# does not put the file on its topic. Accepted cost: a module name built at run
# time is not seen.
_IMPORT_LINE_RE = re.compile(
    r'^\s*(?:import|from|use|using|require|require_relative|extern\s+crate)\b'
    r'|^\s*(?:\}|export\b.*)\s*\bfrom\b'
    r'|^\s*#\s*include\b'
    r'|^\s*(?:[\w.]+\s+)?"[^"\s]+"\s*$'
)
_DYNAMIC_IMPORT_RE = re.compile(r"""\b(?:require|import)\s*\(\s*["']([^"'\s]+)["']""")
_QUOTED_RE = re.compile(r"""["']([^"'\s]+)["']""")


class SkillRequirement(NamedTuple):
    skill: str
    reason: str


def skill_requirements() -> dict:
    """The table in hooks/data/skill_requirements.json, read once per process.

    An unreadable or malformed table yields {}: the skill gate then requires
    governance alone, which is how it behaved before the table existed. Denying
    every write over a broken install would leave no way out but editing the
    plugin, and the structural tests already fail on a malformed table."""
    global _skill_requirements_cache
    if _skill_requirements_cache is None:
        try:
            with open(_SKILL_REQUIREMENTS_PATH, encoding='utf-8') as f:
                data = json.load(f)
            _skill_requirements_cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _skill_requirements_cache = {}
    return _skill_requirements_cache


def _requirement_text(tool_name: str, tool_input: dict, command: str) -> str:
    """The whole text a call writes, read for import lines and code patterns.
    Not the secret scan's added-lines view: an import kept by an edit still
    puts the file on its topic."""
    if tool_name == 'Write':
        return str(tool_input.get('content') or '')
    if tool_name == 'Edit':
        return str(tool_input.get('new_string') or '')
    if tool_name == 'MultiEdit':
        edits = tool_input.get('edits')
        if isinstance(edits, list):
            return '\n'.join(str(e.get('new_string') or '') for e in edits if isinstance(e, dict))
        return ''
    if tool_name == 'Bash':
        return str(tool_input.get('command') or command or '')
    return ''


def _write_paths(tool_name: str, tool_input: dict, command: str, root: str) -> list[str]:
    """Every destination of the write. An editing tool's file counts whatever its
    extension, so a Dockerfile or a Prisma schema reaches the topic rows. A shell
    command yields its source-file targets plus the other files it writes by a
    redirect or `tee` (`cat > Dockerfile <<EOF`); a non-source file it copies,
    moves or downloads is not read, a gap the topic rows accept."""
    if tool_name in CODE_WRITE_TOOLS:
        path = str(tool_input.get('file_path') or tool_input.get('path')
                   or tool_input.get('target_file') or '')
        if not path:
            return []
        return [os.path.normpath(os.path.join(root, os.path.expanduser(path)))]
    if tool_name == 'Bash':
        paths = code_write_targets(tool_name, {**tool_input, '_cwd': root}, command)
        base = _command_root(command, root) or root
        for target in redirect_code_targets(command, source_only=False) or ():
            if not _ends_with_code_ext(target) and target != '/dev/null':
                paths.append(os.path.normpath(os.path.join(base, os.path.expanduser(target))))
        return paths
    return []


def _read_source(path: str) -> str:
    # A regular file only: opening a named pipe at the written path would block
    # the hook until its timeout.
    if not os.path.isfile(path):
        return ''
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read(_SOURCE_READ_LIMIT)
    except OSError:
        return ''


def _project_relative(path: str, root: str) -> str:
    real, base = os.path.realpath(path), os.path.realpath(root) if root else ''
    if base and (real == base or real.startswith(base + os.sep)):
        return os.path.relpath(real, base).replace(os.sep, '/')
    return path.replace(os.sep, '/')


def _nearest_manifest(start: str, name: str, root: str) -> str:
    """The manifest called `name` closest above `start`: the one that governs the
    file in a monorepo. The walk stops at the project root, at a repository
    boundary, or after a fixed number of levels."""
    stop = os.path.realpath(root) if root else ''
    current = os.path.realpath(start)
    for _ in range(_MANIFEST_WALK_LIMIT):
        if '*' in name:
            hits = sorted(glob.glob(os.path.join(glob.escape(current), name)))
            if hits:
                return hits[0]
        elif os.path.isfile(os.path.join(current, name)):
            return os.path.join(current, name)
        parent = os.path.dirname(current)
        if current == stop or os.path.exists(os.path.join(current, '.git')) or parent == current:
            break
        current = parent
    return ''


def _manifest_declares(manifest: str, dependency: str, cache: dict[str, object]) -> bool:
    """Whether `manifest` declares `dependency`. A package.json is read by its
    dependency sections, so a script or description mentioning the name does not
    count; any other manifest format is matched as a whole name in its text."""
    if manifest not in cache:
        text = _read_source(manifest)
        if os.path.basename(manifest) == 'package.json':
            names: set[str] = set()
            try:
                data = json.loads(text)
            except ValueError:
                data = {}
            if isinstance(data, dict):
                for section in _PACKAGE_JSON_SECTIONS:
                    deps = data.get(section)
                    if isinstance(deps, dict):
                        names.update(str(k) for k in deps)
            cache[manifest] = names
        else:
            cache[manifest] = text
    declared = cache[manifest]
    if isinstance(declared, set):
        return dependency in declared
    return bool(re.search(r'(?<![\w.-])' + re.escape(dependency) + r'(?![\w-])', str(declared)))


def _imported_name(text: str, names: list[str]) -> str:
    """The first of `names` that an import line of `text` names. Inside a quoted
    module path the name must be the whole path or its prefix up to a `/`, so
    `./kafka` or `kafkajs` is not `kafka`; an unquoted import (Java, C#, Rust,
    Python) matches the name as a dotted or `::` prefix of the imported path."""
    quoted: list[str] = []
    bare: list[str] = []
    for line in text.splitlines():
        quoted.extend(_DYNAMIC_IMPORT_RE.findall(line))
        if not _IMPORT_LINE_RE.search(line):
            continue
        found = _QUOTED_RE.findall(line)
        if found:
            quoted.extend(found)
        else:
            bare.append(line)
    bare_text = '\n'.join(bare)
    for name in names:
        if any(q == name or q.startswith(name + '/') for q in quoted):
            return name
        if bare_text and re.search(r'(?<![\w@./-])' + re.escape(name) + r'(?![\w-])', bare_text):
            return name
    return ''


_ARCH_ENTRY_LIMIT = 200
_ARCH_SCAN_DIR_LIMIT = 4000
_ARCH_SCAN_DEPTH = 6
_ARCH_NOTICE_LIMIT = 6
_ARCH_SCAN_SKIP = frozenset({'node_modules', 'vendor', 'dist', 'build', 'target', 'out',
                             '__pycache__', 'venv', 'coverage'})


class ArchitectureMatch(NamedTuple):
    skill: str
    name: str
    base: str
    evidence: str


def _architecture_rows(table: dict) -> list[dict]:
    rows = table.get('architectures')
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get('skill')]


def _child_names(directory: str, limit: int) -> tuple[list[str], list[str]]:
    """(subdirectories, files) directly inside `directory`, reading at most
    `limit` entries. An unreadable directory has neither."""
    dirs: list[str] = []
    files: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= limit:
                    break
                try:
                    (dirs if entry.is_dir(follow_symlinks=False) else files).append(entry.name)
                except OSError:
                    continue
    except OSError:
        pass
    return dirs, files


def _module_evidence(container: str, row: dict, cache: dict[tuple[str, str], str]) -> str:
    """'<module>/<entry>' for the first module of `container` with the row's
    shape, '' when none has it."""
    key = (container, str(row.get('name') or row.get('skill')))
    if key in cache:
        return cache[key]
    wanted_dirs = {str(d) for d in row.get('module_dirs', ())}
    patterns = [str(p) for p in row.get('module_files', ())]
    evidence = ''
    modules, _ = _child_names(container, _ARCH_ENTRY_LIMIT)
    for module in sorted(modules):
        subdirs, files = _child_names(os.path.join(container, module), _ARCH_ENTRY_LIMIT)
        hit = (next((d for d in sorted(subdirs) if d in wanted_dirs), '')
               or next((f for f in sorted(files)
                        if any(fnmatch.fnmatchcase(f, p) for p in patterns)), ''))
        if hit:
            evidence = f'{module}/{hit}'
            break
    cache[key] = evidence
    return evidence


def _architecture_at(directory: str, row: dict, cache: dict[tuple[str, str], str]) -> str:
    """What shows `directory` to be the base of the row's structure, '' if it is not."""
    layers = [str(layer) for layer in row.get('layers', ())]
    if layers:
        if all(os.path.isdir(os.path.join(directory, layer)) for layer in layers):
            return ', '.join(layers)
        return ''
    container = str(row.get('container') or '')
    if container and os.path.basename(directory) == container:
        hit = _module_evidence(directory, row, cache)
        return f'{container}/{hit}' if hit else ''
    return ''


def _ancestors(path: str, root: str) -> list[str]:
    """The directories above `path`, nearest first, up to and including `root`;
    [] for a path outside it."""
    base = os.path.realpath(root)
    current = os.path.realpath(os.path.dirname(path))
    if current != base and not current.startswith(base + os.sep):
        return []
    chain = [current]
    while current != base:
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        chain.append(current)
    return chain


def architecture_matches(path: str, root: str, table: dict) -> list[ArchitectureMatch]:
    """Each architecture row whose structure holds the file at `path`, with the
    nearest base for that row. A container row needs the file inside one of its
    modules, so a file sitting directly in `modules/` matches none."""
    cache: dict[tuple[str, str], str] = {}
    chain = _ancestors(path, root or os.getcwd())
    found: list[ArchitectureMatch] = []
    for row in _architecture_rows(table):
        for depth, directory in enumerate(chain):
            if row.get('container') and depth == 0:
                continue
            evidence = _architecture_at(directory, row, cache)
            if evidence:
                found.append(ArchitectureMatch(str(row['skill']), str(row.get('name') or row['skill']),
                                               directory, evidence))
                break
    return found


def _scan_architectures(root: str) -> tuple[list[ArchitectureMatch], bool]:
    """Every base of a known architecture in the tree under `root`, and whether
    the scan stopped at its directory limit before seeing the whole tree."""
    rows = _architecture_rows(skill_requirements())
    if not rows:
        return [], False
    cache: dict[tuple[str, str], str] = {}
    found: list[ArchitectureMatch] = []
    stack = [(os.path.realpath(root), 0)]
    visited = 0
    while stack:
        if visited >= _ARCH_SCAN_DIR_LIMIT:
            return found, True
        directory, depth = stack.pop()
        visited += 1
        for row in rows:
            evidence = _architecture_at(directory, row, cache)
            if evidence:
                found.append(ArchitectureMatch(str(row['skill']), str(row.get('name') or row['skill']),
                                               directory, evidence))
        if depth >= _ARCH_SCAN_DEPTH:
            continue
        subdirs, _ = _child_names(directory, _ARCH_SCAN_DIR_LIMIT)
        for name in sorted(subdirs, reverse=True):
            if not name.startswith('.') and name not in _ARCH_SCAN_SKIP:
                stack.append((os.path.join(directory, name), depth + 1))
    return found, False


def _shown_base(base: str, root: str) -> str:
    where = _project_relative(base, root)
    return 'the project root' if where == '.' else where


def _contains(outer: str, inner: str) -> bool:
    return inner == outer or inner.startswith(outer + os.sep)


def architecture_notice(cwd: str) -> str:
    """Session context naming each architecture found in the project and the
    skill a write under it requires, '' when none is found. Two structures where
    one sits inside the other are named as overlapping: a file under both needs
    neither, so the model is told to confirm the architecture there."""
    root = cwd or os.getcwd()
    found, truncated = _scan_architectures(root)
    if not found:
        if truncated:
            return (f'[playbook:architecture] The scan stopped after {_ARCH_SCAN_DIR_LIMIT} '
                    'directories without finding a known architecture; the gate still checks '
                    'each write against the structure above it.')
        return ''

    def shown(match: ArchitectureMatch) -> str:
        return f'{match.name} at {_shown_base(match.base, root)} ({match.evidence})'

    listed = found[:_ARCH_NOTICE_LIMIT]
    skills = sorted({m.skill for m in found})
    if len(skills) == 1:
        text = (f'[playbook:architecture] Detected {"; ".join(shown(m) for m in listed)}. '
                f'A source write under it requires {skills[0]}: load it before writing code there.')
    else:
        text = (f'[playbook:architecture] Architectures found: {"; ".join(shown(m) for m in listed)}. '
                'A source write requires the skill of the structure it sits under.')
    if len(found) > len(listed):
        text += f' {len(found) - len(listed)} more not listed.'
    overlaps = sorted({_project_relative(inner.base, root)
                       for outer in found for inner in found
                       if outer.skill != inner.skill and _contains(outer.base, inner.base)})
    if overlaps:
        text += (f' Overlapping structures at {", ".join(overlaps)}: a file there matches two '
                 'architectures and the gate requires neither. Confirm with the user which one '
                 'applies before changing the structure there.')
    if truncated:
        text += (f' The scan stopped after {_ARCH_SCAN_DIR_LIMIT} directories, so a structure '
                 'deeper in the tree is not listed; the gate still checks each write.')
    return text


def _requirements_for_path(path: str, written: str, root: str, table: dict,
                           manifest_cache: dict[str, object]) -> list[SkillRequirement]:
    is_source = _ends_with_code_ext(path)
    ext = os.path.splitext(path)[1].lower()
    shown = _project_relative(path, root)
    found: list[SkillRequirement] = []

    language = ''
    languages = table.get('languages')
    if isinstance(languages, dict):
        for name, row in languages.items():
            if isinstance(row, dict) and ext in row.get('extensions', ()):
                language = name
                if is_source and row.get('skill'):
                    found.append(SkillRequirement(str(row['skill']), f'{name} file {shown}'))
                break
    if is_source:
        found[:0] = [SkillRequirement(str(s), 'every source write')
                     for s in table.get('companions', ())]

    frameworks: set[str] = set()
    if is_source and language:
        for row in table.get('frameworks', ()):
            if not isinstance(row, dict) or language not in row.get('languages', ()):
                continue
            for manifest_name, deps in (row.get('manifests') or {}).items():
                manifest = _nearest_manifest(os.path.dirname(path), manifest_name, root)
                dep = next((d for d in deps if manifest and _manifest_declares(manifest, d, manifest_cache)), '')
                if dep:
                    frameworks.add(str(row['skill']))
                    found.append(SkillRequirement(
                        str(row['skill']), f'{dep} in {_project_relative(manifest, root)}'))
                    break

    if is_source:
        matches = architecture_matches(path, root, table)
        if len({m.skill for m in matches}) == 1:
            match = matches[0]
            found.append(SkillRequirement(
                match.skill,
                f'{match.name} layout at {_shown_base(match.base, root)} ({match.evidence})'))

    text = (written + '\n' + _read_source(path)) if is_source else ''
    for row in table.get('topics', ()):
        if not isinstance(row, dict) or not (is_source or row.get('non_code')):
            continue
        if row.get('when_framework') and row['when_framework'] not in frameworks:
            continue
        reason = ''
        for pattern in row.get('paths', ()):
            target = shown if '/' in pattern else os.path.basename(path)
            if fnmatch.fnmatchcase(target, pattern):
                reason = f'{shown} matches {pattern}'
                break
        if not reason and text:
            name = _imported_name(text, list(row.get('imports', ())))
            if name:
                reason = f'{shown} imports {name}'
        if not reason and text:
            for pattern in row.get('patterns', ()):
                try:
                    if re.search(pattern, text):
                        reason = f'{shown} contains {pattern}'
                        break
                except re.error:
                    continue
        if reason:
            found.append(SkillRequirement(str(row['skill']), reason))
    return found


def required_skills(tool_name: str, tool_input: dict, command: str, cwd: str) -> list[SkillRequirement]:
    """The skills this write needs loaded, each with the reason it applies, in
    table order and without repeats. Throwaway destinations (temp, git-ignored)
    need none. See hooks/data/skill_requirements.json for the rows."""
    table = skill_requirements()
    if not table:
        return []
    root = cwd or os.getcwd()
    written = _requirement_text(tool_name, tool_input, command)
    manifest_cache: dict[str, object] = {}
    seen: dict[str, SkillRequirement] = {}
    for path in _write_paths(tool_name, tool_input, command, root):
        found = _requirements_for_path(path, written, root, table, manifest_cache)
        # Throwaway is checked last: it runs `git check-ignore`, and most shell
        # redirects (a log, /dev/stderr) require nothing anyway.
        if not found or is_throwaway_write(path, root):
            continue
        for req in found:
            seen.setdefault(req.skill, req)
    return list(seen.values())


SUSPEND_SKILL = 'playbook-off'
RESUME_SKILLS = ('playbook-on', 'engineering-playbook')

_SUSPEND_RE = re.compile(re.escape(SUSPEND_SKILL))
_RESUME_RE = re.compile('|'.join(re.escape(s) for s in RESUME_SKILLS))


# Codex transcript entry types (payload.type at the top level) that are
# Codex's own bookkeeping/environment snapshots, not model or user
# expression. Confirmed by enumerating every type observed across live
# Codex 0.147.0 sessions. world_state in particular re-serializes the skill
# catalog (including SUSPEND_SKILL's own name) and turn/session metadata —
# none of these are a place a real invocation or confirmation would appear.
_NON_EXPRESSIVE_TYPES = frozenset({'world_state', 'turn_context', 'session_meta', 'event_msg'})


def _drop_non_expressive_lines(transcript_lines: list[str]) -> list[str]:
    """Filter hook-injected and Codex-internal bookkeeping lines out of a raw
    Codex transcript tail before scanning it for trigger strings.

    Three things this excludes, all confirmed via live Codex 0.147.0
    captures: (1) response_item entries whose payload.type is not "message"
    — reasoning, function_call, and function_call_output are model/tool
    bookkeeping, not user or assistant expression, and can re-surface the
    trigger strings this module scans for (e.g. tool output echoing
    SUSPEND_SKILL or SPEC_TOKEN) without a genuine invocation or
    confirmation; (2) response_item "message" entries with payload.role ==
    "developer" — SessionStart's and UserPromptSubmit's additionalContext,
    which documents those same literal strings as instructional examples,
    not as an actual confirmation or invocation; (3) entries whose top-level
    type is in _NON_EXPRESSIVE_TYPES — world_state was observed to
    re-serialize the skill catalog, which also contains SUSPEND_SKILL's name
    as a plain listing. Without this filter, has_spec_token and is_suspended
    would find their own trigger strings in every session's own
    instructions/state and misfire from the first tool call, independent of
    anything the model or user did.

    Lines that are not valid JSON, or valid JSON that does not match this
    envelope shape at all (e.g. a test fixture using a flat
    {"role": ..., "content": ...} format instead of Codex's real
    {"type": ..., "payload": {...}} envelope), are kept as-is — fail-open,
    consistent with the rest of this module, and keeps non-Codex transcript
    fixtures scannable."""
    kept = []
    for line in transcript_lines:
        try:
            obj = json.loads(line)
        except Exception:
            kept.append(line)
            continue
        obj_type = obj.get('type') if isinstance(obj, dict) else None
        if obj_type in _NON_EXPRESSIVE_TYPES:
            continue
        if obj_type == 'response_item':
            payload = obj.get('payload')
            if not isinstance(payload, dict):
                continue
            if payload.get('type') != 'message':
                continue
            if payload.get('role') == 'developer':
                continue
        kept.append(line)
    return kept


def has_spec_token(transcript_lines: list[str]) -> bool:
    text = '\n'.join(_drop_non_expressive_lines(transcript_lines))
    return SPEC_TOKEN in text


def is_suspended(transcript_lines: list[str]) -> bool:
    """Whether governance is currently suspended, from a raw transcript scan.

    Schema-agnostic: unlike context_guard.py's _scan_skills (which parses
    Claude's transcript entries for actual Skill-tool tool_use blocks named
    playbook-off/-on), this does a plain substring scan for the
    skill names anywhere in the filtered lines and takes whichever appears
    last. Codex has no Skill tool — "invoking" a skill there means the model
    reads the SKILL.md file directly or states it in response text, and
    Codex's transcript entry schema for a file read is not confirmed beyond
    what _drop_non_expressive_lines excludes, so a fully structured
    per-entry parse is not reliably portable the way it is for Claude.
    Correct as long as the skill name does not appear in a kept line for
    unrelated reasons."""
    suspended = False
    for line in _drop_non_expressive_lines(transcript_lines):
        if _SUSPEND_RE.search(line):
            suspended = True
        elif _RESUME_RE.search(line):
            suspended = False
    return suspended


AUTHORIZATION_HINT = (
    'Nothing typed in the chat lifts this block, and you cannot authorize it yourself.'
)


def _destructive_block(reason: str, effect: str, part: str = '') -> dict:
    """`effect` names what the command destroys, for the text the user reads
    (playbook_i18n `destroys_<effect>`); `part` is the simple command that
    does it, shown to the user instead of the start of a longer command."""
    return {'block': True, 'effect': effect, 'part': part,
            'msg': f'[playbook:safety] {reason} {AUTHORIZATION_HINT}'}


_RECURSIVE_RM_OPTION_RE = re.compile(r'-[a-zA-Z]*[rR][a-zA-Z]*')
# Where the words of an `rm` run as an argument end: `find -exec rm -r {} ;`.
_ARGUMENT_COMMAND_END = frozenset({';', '+', '--'})


def _recursive_rm(words: list[str]) -> str:
    """The `rm -r` in a simple command, as the words from `rm` on; '' when
    there is none. The `rm` may be the program or run as an argument of it
    (`find -exec`, `watch`, `flock f`, `docker exec ctr`), and only its own
    options count: GNU rm reads them anywhere before `--`, so `rm dir -rf`
    is recursive. A program that only prints or searches runs nothing."""
    if words[0].rsplit('/', 1)[-1] in _TEXT_ONLY_WORDS:
        return ''
    for at, word in enumerate(words):
        if word.rsplit('/', 1)[-1] != 'rm':
            continue
        for option in words[at + 1:]:
            if option in _ARGUMENT_COMMAND_END:
                break
            if option == '--recursive' or _RECURSIVE_RM_OPTION_RE.fullmatch(option):
                return shlex.join(words[at:])
    return ''


def token_deletion(command: str, disposable_root: str = '') -> tuple[str, str] | None:
    """`(part, effect)` for a recursive `rm` or an `xargs rm` in `command`,
    read per simple command, scripts handed to a shell (`sh -c`, `eval`)
    included; None when there is none. Text that does not tokenize, the whole
    command or a nested script, is read with DELETION_TEXT_RULES instead. An
    `rm` inside `disposable_root` is left out: see `removes_only_under`."""
    read = _raw_simple_commands(command)
    texts = [command] if read is None else read[1]
    for text in texts:
        for pattern, effect in DELETION_TEXT_RULES:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0).strip(), effect
    if read is None:
        return None
    for segment, nested in read[0]:
        words = command_words(segment)
        if not words or words[0].rsplit('/', 1)[-1] in _TEXT_ONLY_WORDS:
            continue
        if (not nested and words == segment
                and removes_only_under(segment, disposable_root)):
            continue
        bases = [word.rsplit('/', 1)[-1] for word in segment]
        if 'xargs' in bases and 'rm' in bases[bases.index('xargs') + 1:]:
            return shlex.join(segment), 'mass_delete'
        part = _recursive_rm(words)
        if part:
            return part, 'delete_tree'
    return None


# Commands whose arguments are text being searched for, never a command: a
# grep pattern naming `find ... -delete` is not a deletion.
_SEARCH_TOOLS = frozenset({'grep', 'egrep', 'fgrep', 'zgrep', 'rg', 'ag', 'ack'})


def _searches_only(words: list[str]) -> bool:
    base = words[0].rsplit('/', 1)[-1]
    if base == 'git':
        sub = _GIT_GLOBAL_OPTS_RE.sub('git ', ' '.join(words)).split()[1:2]
        return sub == ['grep']
    return base in _SEARCH_TOOLS


def removes_only_under(words: list[str], root: str) -> bool:
    """Whether `words` is an `rm` whose every operand is a literal absolute
    path inside `root` (the session scratchpad), resolved through symlinks:
    `rm -rf <scratchpad>/link/` follows the link and is not inside. A glob, a
    variable, a relative path or no operand at all is never inside."""
    if not root or words[0].rsplit('/', 1)[-1] != 'rm':
        return False
    operands: list[str] = []
    options_done = False
    for word in words[1:]:
        if not options_done and word == '--':
            options_done = True
        elif not options_done and word.startswith('-'):
            continue
        else:
            operands.append(word)
    real_root = os.path.realpath(root)
    for operand in operands:
        if not (_is_literal_path(operand) and os.path.isabs(operand)):
            return False
        real = os.path.realpath(operand)
        if os.path.commonpath((real, real_root)) != real_root:
            return False
    return bool(operands)


def _pattern_lines(command: str) -> list[tuple[str, str]]:
    """What the destructive patterns read, each with the simple command it
    came from as the user is shown it: each simple command on a line of its
    own, quotes removed, search tools left out; once as written and once with
    wrappers and git's global options dropped (`sudo git -C . reset --hard`).
    Read as one line, `grep "find .*-exec"; grep "push --delete"` matched
    `find ... -delete` across two commands' arguments. The raw text when the
    command does not tokenize."""
    segments = shell_segments(command)
    if segments is None:
        return [(command, command)]
    lines = []
    for segment in segments:
        words = command_words(segment)
        if words and not _searches_only(words):
            shown = shlex.join(segment)
            read = _without_message_text(segment)
            lines += [(shown, ' '.join(read)),
                      (shown, _GIT_GLOBAL_OPTS_RE.sub('git ', ' '.join(command_words(read))))]
    return lines


# A commit, tag, stash or pull request carries prose in its message options; a
# message that mentions `git reset --hard` is text, and read as part of the
# line it asked the user about every commit that explained a fix to the gate.
_MESSAGE_COMMAND_RE = re.compile(
    r'^(?:git\s+(?:commit|tag|stash|merge|revert|notes|am)|gh\s+(?:pr|issue|release|gist))\b')
_MESSAGE_VALUE_OPTIONS = {
    'git': frozenset({'-m', '--message'}),
    'gh': frozenset({'--title', '-t', '--body', '-b', '--notes', '-n', '--description', '-d'}),
}
_MESSAGE_CLUSTER_RE = re.compile(r'-[a-zA-Z]*m')


def _without_message_text(segment: list[str]) -> list[str]:
    """`segment` without the text of its message options (`-m "..."`,
    `--message=...`, `--title`, `--body`, ...), when it is a command that takes a
    message; unchanged otherwise. Only those values: every other word of the
    command is still read, so a destructive command after `&&` or a `-c`
    script is untouched, and so is a substitution the shell runs from inside
    double quotes (the shell has already split it into its own command)."""
    words = command_words(segment)
    if not words or not _MESSAGE_COMMAND_RE.match(
            _GIT_GLOBAL_OPTS_RE.sub('git ', ' '.join(words))):
        return segment
    options = _MESSAGE_VALUE_OPTIONS.get(words[0].rsplit('/', 1)[-1], frozenset())
    kept: list[str] = []
    skip = False
    for word in segment:
        if skip:
            skip = False
        elif word in options or (words[0] == 'git' and _MESSAGE_CLUSTER_RE.fullmatch(word)):
            skip = True
        elif not word.startswith(('--message=', '--title=', '--body=', '--notes=',
                                  '--description=')):
            kept.append(word)
    return kept


def check_destructive(command: str, raw: str = '', cwd: str = '', _depth: int = 0,
                      disposable_root: str = '',
                      tolerate: frozenset[str] = frozenset()) -> dict | None:
    """Flag a command that destroys work; the adapter decides how the user
    approves it (Claude Code: a permission dialog; Codex: never in-session).

    `command` is the runnable command (heredoc bodies and inline scripts
    stripped). Three readings of it, each closing a way past the one before:

    1. the patterns and the `rm` tokens, per simple command in its
       shell-normalized form, so quoting the command word or adding git global
       options does not hide it; a variable command word is read as every
       literal value the command gives it (`resolve_literal_commands`);
    2. recursive deletion through an interpreter API in a `-c`/`-e` script,
       read in `raw` — the full command — because that source is stripped
       from `command` before step 1;
    3. the scripts the command runs (`script_sources`): a Python or other
       interpreter script for the same APIs, a shell script as a command. Of
       the script files, only those git does not track: see `script_sources`.

    A program the command does not show (`$(...)`, `eval "$c"`, a variable with
    no literal value) is not flagged: only what is shown to be destructive asks
    (decided by the user for the auto-mode consent work). Inside a script file
    (`_depth` > 0) variables are not resolved: a variable command word there
    (`exec "$@"`) is the script's own logic.

    `disposable_root` is the session's own scratchpad: an `rm` of literal paths
    inside it deletes only what the session made there, and needs no consent
    (decided by the user for 4.0.0; a build directory or node_modules still
    does).

    `tolerate` names effects the caller has made recoverable (see
    `snapshot_before_discard`): the pattern hits of the command as typed with
    those effects are skipped. A script the command runs is checked in full."""
    command = strip_heredoc_bodies(command)
    expanded = resolve_literal_commands(command) if _depth == 0 else command
    for shown, line in _pattern_lines(expanded):
        for pattern, effect in DESTRUCTIVE_RULES:
            if effect in tolerate:
                continue
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                return _destructive_block(
                    f'Destructive operation detected (`{match.group(0).strip()}`). Show the '
                    'user the exact command and what it destroys.', effect, shown)
    deletion = token_deletion(expanded, disposable_root)
    if deletion:
        part, effect = deletion
        return _destructive_block(
            f'Destructive operation detected (`{part[:120]}`). Show the user the exact '
            'command and what it destroys.', effect, part)
    # Heredoc bodies that are data (a PR body, a doc) are left out: one quoting
    # `node -e "fs.rmSync(dir, opts)"` is text. A heredoc fed to a shell stays.
    for payload in inline_payloads(strip_heredoc_bodies(raw or command)):
        call = _recursive_delete_in_text(payload)
        if call:
            return _destructive_block(
                f'Recursive deletion in an interpreter script (`{call}`). '
                'Show the user what it deletes.', 'script_delete')
    if _depth == 0:
        for line in interpreter_shell_commands(raw or command, cwd):
            inner = check_destructive(strip_heredoc_bodies(line), line, cwd, _SCRIPT_MAX_DEPTH)
            if inner:
                # The part is a line of the script, not of the command the
                # user is asked about: the dialog shows that command instead.
                return {**inner, 'part': '', 'msg': inner['msg'].replace(
                    '[playbook:safety] ', '[playbook:safety] In a command an interpreter script '
                    'runs: ', 1)}
    sources = script_sources(raw or command, cwd, untracked_only=True)
    if expanded != command:
        # `S=./clean.sh; $S` runs the script the literal names: read it too.
        seen = {source.origin for source in sources}
        sources += [source for source in script_sources(expanded, cwd, untracked_only=True)
                    if source.origin not in seen]
    for source in sources:
        if source.language == 'shell':
            if _depth >= _SCRIPT_MAX_DEPTH:
                continue
            text = _shell_script_text(source.text)
            inner = check_destructive(strip_inline_payloads(text), text, cwd, _depth + 1)
            if inner:
                return {**inner, 'part': '', 'msg': inner['msg'].replace(
                    '[playbook:safety] ', f'[playbook:safety] In the script `{source.origin}`: ', 1)}
            continue
        call = _recursive_delete_call(source)
        if call:
            return _destructive_block(
                f'Recursive deletion in the script `{source.origin}` (`{call}`). Show the '
                'user what it deletes.', 'script_delete')
    return None


# Effects of a git command that only overwrite tracked content: what they destroy
# is what `git stash create` captures, so a snapshot makes them recoverable.
# `clean` (untracked files), branch deletion and the rest are not.
SNAPSHOT_EFFECTS = frozenset({'discard_changes', 'reset_hard'})
_SNAPSHOT_REF = 'refs/playbook/snapshots/'
_SNAPSHOT_KEPT = 20
_SNAPSHOT_REPOS_MAX = 5
_SNAPSHOT_TIMEOUT = 4.0
_PLAIN_VALUE_RE = re.compile(r'[\w@%+=:,./~-]+')


class Snapshot(NamedTuple):
    """Uncommitted tracked changes of `repo`, kept as the commit `sha` under
    `ref`. `sha` is empty when the repository had none to lose."""
    repo: str
    ref: str
    sha: str


def _single_valued_variables(command: str) -> str:
    """`command` with each `$NAME` or `${NAME}` the text gives exactly one
    plain literal replaced by it (`C=/srv/app; git -C $C checkout -- f`), so a
    repository named through a variable can be read. A name with several
    values, or a value from a substitution or the environment, stays as it is."""
    outer, _inner = _lift_substitutions(command.replace('\\\n', ' '))
    statements = _statements(outer)
    if not statements:
        return command
    values = {name: found[0] for name, found in _literal_candidates(statements).items()
              if len(found) == 1 and _PLAIN_VALUE_RE.fullmatch(found[0])}
    if not values:
        return command
    return _VAR_REF_RE.sub(lambda m: values.get(m.group(1) or m.group(2), m.group(0)), command)


def _prune_snapshots(repo: str) -> None:
    listed = _git_out(repo, 'for-each-ref', '--sort=-refname', '--format=%(refname)',
                      _SNAPSHOT_REF, timeout=_SNAPSHOT_TIMEOUT)
    for ref in (listed or '').split()[_SNAPSHOT_KEPT:]:
        _git_out(repo, 'update-ref', '-d', ref, timeout=_SNAPSHOT_TIMEOUT)


def _snapshot_repo(repo: str) -> Snapshot | None:
    """Keep the uncommitted tracked changes of `repo` under a ref, without
    touching its working tree or index. None when git cannot (not a repository,
    no commit yet, unmerged paths, no committer identity, out of time)."""
    top = _git_out(repo, 'rev-parse', '--show-toplevel', timeout=_SNAPSHOT_TIMEOUT)
    sha = _git_out(repo, 'stash', 'create', 'playbook: saved before a discard',
                   timeout=_SNAPSHOT_TIMEOUT)
    if not top or sha is None:
        return None
    top, sha = top.strip(), sha.strip()
    if not sha:
        return Snapshot(top, '', '')
    ref = f'{_SNAPSHOT_REF}{int(time.time() * 1000)}-{os.getpid()}'
    # The empty old value makes a name that already exists fail, so a snapshot
    # never overwrites another one: it falls back to asking.
    if _git_out(repo, 'update-ref', ref, sha, '', timeout=_SNAPSHOT_TIMEOUT) is None:
        return None
    _prune_snapshots(repo)
    return Snapshot(top, ref, sha)


def _moves_head(reset_args: list[str]) -> bool:
    """Whether a `git reset` names a commit other than HEAD. That drops commits
    from the branch, which a snapshot of uncommitted changes does not bring back
    (only the reflog does), so it is still asked about."""
    return any(arg != 'HEAD' for arg in reset_args if not arg.startswith('-'))


def snapshot_before_discard(command: str, cwd: str) -> list[Snapshot] | None:
    """Make the discards in `command` recoverable: for each repository a
    `SNAPSHOT_EFFECTS` git call acts on, keep its uncommitted tracked changes
    under `refs/playbook/snapshots/` (`git stash apply <sha>` brings them back).
    The snapshot is a commit, not a stash entry: nothing in the working tree
    moves, so there is nothing to pop afterwards and nothing to conflict with.
    Accepted trade-off: the snapshot is taken before the command runs, so a
    file another tool call of the same response changes in between is saved as
    it was.

    The snapshots that hold something; an empty list when no repository had
    changes to lose. None when the command cannot be made recoverable, and the
    caller keeps asking: a repository decided at run time, one git cannot
    snapshot, a reset to another commit, a discard git_calls does not see, or
    more than `_SNAPSHOT_REPOS_MAX` repositories."""
    calls = git_calls(_single_valued_variables(command), cwd)
    if calls is None:
        return None
    repos: list[str] = []
    for call in calls:
        line = ' '.join(['git', call.sub, *call.args])
        if not any(effect in SNAPSHOT_EFFECTS and re.search(pattern, line, re.IGNORECASE)
                   for pattern, effect in DESTRUCTIVE_RULES):
            continue
        if call.repo is None or (call.sub == 'reset' and _moves_head(call.args)):
            return None
        if call.repo not in repos:
            repos.append(call.repo)
    if not repos or len(repos) > _SNAPSHOT_REPOS_MAX:
        return None
    kept: list[Snapshot] = []
    for repo in repos:
        snapshot = _snapshot_repo(repo)
        if snapshot is None:
            return None
        if snapshot.sha:
            kept.append(snapshot)
    return kept


def attribution_allowed() -> bool:
    """Whether the user switched the attribution gates off for this session.

    Read from the hook's own environment, which a command the model runs cannot
    change: `VAR=1 git push` sets it for git, not for the hook."""
    return os.environ.get(ATTRIBUTION_ALLOW_ENV) == '1'


def _join_literal_dir(root: str, target: str) -> str | None:
    """`root` joined with a directory as written in a command, or None when the
    shell would compute it (a variable or a substitution)."""
    target = target.strip('"\'')
    if '$' in target or '`' in target:
        return None
    return os.path.normpath(os.path.join(root, os.path.expanduser(target)))


def _command_root(command: str, cwd: str) -> str | None:
    """Directory a command starts in: `cwd`, moved by a literal leading `cd`.
    None when the `cd` target is not literal and so cannot be resolved here."""
    root = cwd or os.getcwd()
    cd_match = _CD_PREFIX_RE.match(command or '')
    return _join_literal_dir(root, cd_match.group('dir')) if cd_match else root


# An environment that points git at another repository for the commands after it.
_GIT_REPO_ENV_RE = re.compile(r'(?:^|[\s;&|(])(?:export\s+)?GIT_(?:DIR|WORK_TREE)=')
# git's global options that take the next word as their value.
_GIT_VALUE_OPTS = frozenset({'-c', '--namespace', '--exec-path', '--super-prefix', '--config-env'})


class GitCall(NamedTuple):
    """One git invocation of a command: its subcommand and the words after it,
    and the repository it acts on, or None with the reason when that is
    decided at run time."""
    repo: str | None
    reason: str
    sub: str
    args: list[str]


_DIR_STACK_RE = re.compile(r'[+-]\d+')
# Programs that only print or search the words after them: `echo git push`
# mentions a push, it does not run one.
_TEXT_ONLY_WORDS = _SEARCH_TOOLS | frozenset({'echo', 'printf', 'man', 'which', 'type'})


def _argument_git(words: list[str]) -> int | None:
    """Index of a `git` that the program of `words` runs as its argument
    (`find . -execdir git push \\;`, `watch git push`, `flock f git push`).
    The text pattern this reader replaced matched these by accident; a
    per-command read that skips them lets a push past every push gate. None
    when there is none, when the program only prints or searches, or for
    `eval`, whose arguments `_simple_commands` already reads as a script."""
    base = words[0].rsplit('/', 1)[-1]
    if base == 'eval' or base in _TEXT_ONLY_WORDS:
        return None
    return next((j for j, word in enumerate(words[1:], start=1)
                 if word.rsplit('/', 1)[-1] == 'git'), None)


def git_calls(command: str, cwd: str) -> list[GitCall] | None:
    """Every git invocation in `command`, in order, with its repository: `cwd`,
    moved by each literal `cd`/`pushd` before it and by its own `-C`.

    Reading only a leading `cd` sent `true && cd ../other && git push` to the
    session's repository. The repository is unknown (None) after a `cd` or
    `-C` the shell computes, after `popd`, and for `--git-dir`/`--work-tree`
    or a `GIT_DIR`/`GIT_WORK_TREE` environment. None when `command` does not
    tokenize. A `cd` inside a subshell is carried forward: the directory after
    the subshell may be read wrong, never left unread."""
    read = _simple_commands(command)
    if read is None:
        return None
    root: str | None = cwd or os.getcwd()
    env_repo = bool(_GIT_REPO_ENV_RE.search(command))
    calls: list[GitCall] = []
    for words in read[0]:
        base = words[0].rsplit('/', 1)[-1]
        if base in ('cd', 'pushd'):
            # `cd -` and `pushd +1` go where OLDPWD or the directory stack says.
            if any(w == '-' or _DIR_STACK_RE.fullmatch(w) for w in words[1:]):
                root = None
                continue
            target = next((w for w in words[1:] if not w.startswith('-')), '~')
            root = _join_literal_dir(root, target) if root else None
            continue
        if base == 'popd':
            root = None
            continue
        if base == 'git':
            repo, reason, i = root, '' if root else 'its directory is not a literal path', 1
        else:
            at = _argument_git(words)
            if at is None:
                continue
            # Where it runs is the other program's choice (`find -execdir`).
            repo, reason, i = None, f'git runs as an argument of `{base}`', at + 1
        while i < len(words) and words[i].startswith('-'):
            word = words[i]
            if word == '-C' and i + 1 < len(words):
                repo = _join_literal_dir(repo, words[i + 1]) if repo else None
                reason = '' if repo else (reason or 'its directory is not a literal path')
                i += 2
            elif word.startswith(('--git-dir', '--work-tree')):
                repo, reason = None, 'its repository is set by --git-dir/--work-tree'
                i += 1 if '=' in word else 2
            else:
                i += 2 if word in _GIT_VALUE_OPTS else 1
        if i >= len(words):
            continue
        if env_repo:
            repo, reason = None, 'its repository is set by GIT_DIR/GIT_WORK_TREE'
        calls.append(GitCall(repo, reason, words[i].lower(), words[i + 1:]))
    return calls


def is_git_push(command: str) -> bool:
    """Whether `command` runs `git push`: as a command, not as text an
    argument mentions (`git commit -m "explain git push"`). A command that
    does not tokenize falls back to the text pattern."""
    calls = git_calls(command, '')
    if calls is None:
        return bool(GIT_PUSH_RE.search(command))
    return any(call.sub == 'push' for call in calls)


# Files a publishing command takes its message or body from: `git commit
# -F/--file/--template`, `gh ... --body-file/-F`, and the Azure CLI `@file`
# value syntax on `--description`. A regex rather than an argument parser: a
# match that is not a message file costs one failed open(), a missed one is a
# leak. `-` (stdin) is left to the raw command scan, which already reads the
# heredoc feeding it.
_MESSAGE_FILE_RE = re.compile(
    r"""(?:--body-file|--file|--template|(?<![\w-])-F)(?:=|\s+)("[^"]*"|'[^']*'|[^\s;&|<>()]+)"""
    r"""|--description(?:=|\s+)@("[^"]*"|'[^']*'|[^\s;&|<>()]+)"""
)
_MESSAGE_FILE_MAX_BYTES = 1_000_000


def _message_file_has_attribution(command: str, root: str) -> bool:
    for groups in _MESSAGE_FILE_RE.findall(command):
        raw = next((g for g in groups if g), '').strip('"\'')
        if not raw or raw == '-' or '$' in raw or '`' in raw:
            continue
        path = os.path.join(root, os.path.expanduser(raw))
        if not os.path.isfile(path):  # a FIFO or device would hang the hook
            continue
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                if AI_ATTRIBUTION_RE.search(f.read(_MESSAGE_FILE_MAX_BYTES)):
                    return True
        except OSError:
            continue
    return False


def check_ai_attribution(command: str, cwd: str = '') -> dict | None:
    """Deny AI attribution on anything published into project history.

    Not only the commit: a pull request body is read by the same team, and on a
    single-commit PR the forge pre-fills it from the commit message, so the
    trailer travels even when nobody typed it twice.

    Whether the command publishes is read with incidental heredocs and inline
    scripts stripped -- a script that merely mentions `git commit` is not a
    commit. Whether it carries attribution is read on the RAW command: the
    message is often assembled by a script in that same command (a python
    heredoc writing the file that `git commit -F` then reads), and stripping
    that script is exactly how one shipped. A message or body file the command
    names is read too, for the case where it was written by an earlier call.

    This is the early check. It cannot see a message assembled at run time from
    elsewhere; `check_new_commit_attribution` and `check_push_attribution` read
    the history itself and close that gap."""
    if attribution_allowed():
        return None
    runnable = strip_inline_payloads(strip_heredoc_bodies(command, PUBLISH_TOOLS))
    if not PUBLISH_CMD_RE.search(runnable):
        return None
    root = _command_root(command, cwd)
    if AI_ATTRIBUTION_RE.search(command) or (
            root is not None and _message_file_has_attribution(runnable, root)):
        return {
            'block': True,
            'msg': (
                '[playbook:commit] AI attribution in a command that publishes a commit '
                'message or pull request body (a Co-Authored-By trailer naming Claude or '
                'the Anthropic noreply address, or a "Generated with/by Claude" line). The '
                'whole command is checked, including a script in it that writes the '
                'message and any message or body file it reads. Remove those lines and '
                'retry — some teams restrict AI attribution in project history.'
            ),
        }
    return None


# The PreToolUse hooks get 20 s (hooks/hooks.json, .codex-plugin/hooks.json),
# and a hook killed by its timeout lets the tool call run: a push gate that
# fails closed has to answer before then. Each git call had its own timeout,
# and one push's calls added up past 20 s on a slow repository.
_HOOK_GIT_BUDGET = 15.0
_hook_deadline: float | None = None


def start_hook_clock(budget: float = _HOOK_GIT_BUDGET) -> None:
    """Start the time the git calls of this hook process share. Called by
    each PreToolUse hook's main(); without it (tests, scripts) a call keeps
    only its own timeout."""
    global _hook_deadline
    _hook_deadline = time.monotonic() + budget
    secret_scan.set_deadline(_hook_deadline)


def _time_left(timeout: float) -> float:
    """`timeout`, cut to what is left of the hook's budget; 0 when none is."""
    if _hook_deadline is None:
        return timeout
    return max(0.0, min(timeout, _hook_deadline - time.monotonic()))


def _git_out(cwd: str, *args: str, timeout: float, stdin: str | None = None) -> str | None:
    """stdout of a git command, or None when it failed, timed out, or the
    hook has no time left for it (the caller decides what None means)."""
    timeout = _time_left(timeout)
    if timeout <= 0:
        return None
    try:
        done = subprocess.run(('git', '-C', cwd) + args, input=stdin,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


# One call's share of the hook's git budget (see _HOOK_GIT_BUDGET).
_ATTRIBUTION_LOG_TIMEOUT = 5.0
_ATTRIBUTION_LISTED = 10


def unpublished_attribution(cwd: str, revs: list[str]) -> list[str] | None:
    """Commits reachable from `revs` and from no remote-tracking ref whose
    message carries AI attribution, as `<sha> <subject>` lines (at most
    _ATTRIBUTION_LISTED).

    [] outside a repository, where nothing can be published, and for an empty
    `revs` (a push that only deletes). None when the repository exists but the
    search failed: the caller decides whether that fails closed.

    "No remote has it" is read from the local remote-tracking refs. A commit
    already on a remote that was never fetched reads as unpublished -- a false
    positive on a commit that should not exist under this policy anyway."""
    if not revs or _git_out(cwd, 'rev-parse', '--git-dir', timeout=2) is None:
        return []
    out = _git_out(
        cwd, 'log', '-i', '-E', *(f'--grep={p}' for p in _AI_ATTRIBUTION_GREP),
        f'--max-count={_ATTRIBUTION_LISTED}', '--format=%h %s',
        *revs, '--not', '--remotes', '--',
        timeout=_ATTRIBUTION_LOG_TIMEOUT,
    )
    if out is None:
        return None
    return [line for line in out.splitlines() if line.strip()]


def pushed_tag_attribution(cwd: str, revs: list[str]) -> list[str] | None:
    """Annotated tags a push with these revisions may publish whose message
    carries AI attribution, as `tag <name>` lines.

    A tag is counted when the push names it, sends every tag (`--tags`,
    `--mirror`), or points at a commit the push publishes (what `--follow-tags`
    and `push.followTags` send). The last case over-reads a tag that stays
    local; such a tag should not exist under this policy anyway. None when the
    repository exists but git failed."""
    if not revs or _git_out(cwd, 'rev-parse', '--git-dir', timeout=2) is None:
        return []
    listing = _git_out(cwd, 'for-each-ref', 'refs/tags',
                       '--format=%(refname:short)%00%(objecttype)%00%(*objectname)%00%(contents)%1e',
                       timeout=_ATTRIBUTION_LOG_TIMEOUT)
    if listing is None:
        return None
    tags = [entry.lstrip('\n').split('\0', 3) for entry in listing.split('\x1e')]
    flagged = [(name, target) for name, kind, target, message in (t for t in tags if len(t) == 4)
               if kind == 'tag' and AI_ATTRIBUTION_RE.search(message)]
    if not flagged:
        return []
    every_tag = '--tags' in revs
    named = {rev.removeprefix('refs/tags/') for rev in revs}
    commits: set[str] = set()
    if not every_tag and not all(name in named for name, _ in flagged):
        out = _git_out(cwd, 'rev-list', *revs, '--not', '--remotes', '--',
                       timeout=_ATTRIBUTION_LOG_TIMEOUT)
        if out is None:
            return None
        commits = set(out.split())
    return [f'tag {name}' for name, target in flagged
            if every_tag or name in named or target in commits][:_ATTRIBUTION_LISTED]


class _PushOpt(Enum):
    """What a `git push` option does to what the push publishes."""
    NOTHING = auto()         # publishes what the refspecs name, no more
    OPTIONAL_VALUE = auto()  # as NOTHING; a value only after `=`
    VALUE = auto()           # as NOTHING; its value is the next word, or after `=`
    DRY_RUN = auto()
    DELETE = auto()
    BRANCHES = auto()
    TAGS = auto()
    MIRROR = auto()


# Every option the push gates read, spelled exactly as `git push -h` lists it.
# Any other word starting with `-` makes the push unknown: no prefix
# abbreviation (`--dr`), no short-flag bundle (`-fn`), no `--no-` negation
# (`--no-verify` is an option of its own), so a dry run or a delete seen once
# stays in effect. The cost is a denied push that a retry in plain spelling
# passes. `--recurse-submodules` is left out: it pushes other repositories.
# `--follow-tags` publishes no commit the refspecs do not; its tags are read by
# `pushed_tag_attribution`, which misses one on an already-published commit,
# as it does for `push.followTags`, which a plain push obeys anyway.
_PUSH_OPTIONS: dict[str, _PushOpt] = {
    **dict.fromkeys(('-v', '--verbose', '-q', '--quiet', '--porcelain', '--progress',
                     '-u', '--set-upstream', '-f', '--force', '--force-if-includes',
                     '--atomic', '--no-verify', '--verify', '--prune', '--thin',
                     '--follow-tags', '-4', '--ipv4', '-6', '--ipv6'), _PushOpt.NOTHING),
    **dict.fromkeys(('--force-with-lease', '--signed'), _PushOpt.OPTIONAL_VALUE),
    **dict.fromkeys(('-o', '--push-option', '--receive-pack', '--exec', '--repo'),
                    _PushOpt.VALUE),
    '-n': _PushOpt.DRY_RUN, '--dry-run': _PushOpt.DRY_RUN,
    '-d': _PushOpt.DELETE, '--delete': _PushOpt.DELETE,
    '--all': _PushOpt.BRANCHES, '--branches': _PushOpt.BRANCHES,
    '--tags': _PushOpt.TAGS,
    '--mirror': _PushOpt.MIRROR,
}
_PUSH_RUN_TIME = 'a refspec is built at run time'
# A shell redirection among the words (`2>/dev/null`, `> log`, `2>`). Not a
# refspec: read as one, `git log` fails on it and the push fails closed.
_REDIRECT_RE = re.compile(r'^\d*(?:>>?|<)')
_REDIRECT_ONLY_RE = re.compile(r'^\d*(?:>>?|<)$')


def _unknown_push_option(word: str) -> str:
    return (f'it does not read the option `{word}`: spell each option in full and on its '
            'own, as `git push -h` lists it (`-f -n`, not `-fn`; `--dry-run`, not `--dr`); '
            'a `--no-` negation and `--recurse-submodules` are never read, so drop them or '
            'ask the user to run the push')


def push_revisions(args: list[str]) -> list[str] | None:
    """Revisions a `git push` with these arguments (the words after `push`)
    would publish, as `git log` revision arguments; None when they cannot be
    known before the shell runs it (`read_push` says why)."""
    return read_push(args)[0]


def read_push(args: list[str]) -> tuple[list[str] | None, str]:
    """The revisions a push with these arguments publishes, or None with the
    reason.

    No refspec means the current branch, which is what `push.default` simple,
    current and upstream all push. `src:dst` publishes `src`, `:dst` and
    `--delete` publish nothing, a bare `:` publishes every matching branch
    (`--branches`), a glob goes through `--glob`, and a dry run publishes
    nothing. None for a word built at run time (a variable or a command
    substitution), an option outside `_PUSH_OPTIONS`, and a refspec whose
    source starts with `-` (after `--`, or behind `+`), which `git log` would
    take as an option."""
    revs: list[str] = []
    positional: list[str] = []
    dry_run = deleting = skip_next = options_ended = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT_RE.match(arg):
            skip_next = bool(_REDIRECT_ONLY_RE.match(arg))  # `> file`: the file too
            continue
        if '$' in arg or '`' in arg:
            return None, _PUSH_RUN_TIME
        if options_ended or not arg.startswith('-'):
            positional.append(arg)
            continue
        if arg == '--':
            options_ended = True
            continue
        name, equals, _ = arg.partition('=') if arg.startswith('--') else (arg, '', '')
        meaning = _PUSH_OPTIONS.get(name)
        if meaning is None or (equals and meaning not in (_PushOpt.VALUE,
                                                          _PushOpt.OPTIONAL_VALUE)):
            return None, _unknown_push_option(arg)
        if meaning is _PushOpt.VALUE:
            skip_next = not equals
        elif meaning is _PushOpt.DRY_RUN:
            dry_run = True
        elif meaning is _PushOpt.DELETE:
            deleting = True
        elif meaning is _PushOpt.BRANCHES:
            revs.append('--branches')
        elif meaning is _PushOpt.TAGS:
            revs.append('--tags')
        elif meaning is _PushOpt.MIRROR:
            revs += ['--branches', '--tags']
    if dry_run:
        return [], ''
    refspecs = positional[1:]  # positional[0] is the remote
    if deleting:
        return revs, ''
    # `git push <remote> tag <name>` is git's spelling of refs/tags/<name>.
    specs: list[str] = []
    for at, spec in enumerate(refspecs):
        if at > 0 and refspecs[at - 1] == 'tag' and (at < 2 or refspecs[at - 2] != 'tag'):
            specs[-1] = f'refs/tags/{spec}'
        else:
            specs.append(spec)
    for spec in specs:
        bare = spec[1:] if spec.startswith('+') else spec
        if bare == ':':  # git's "matching" refspec: every branch the remote also has
            revs.append('--branches')
            continue
        src = bare.split(':', 1)[0]
        if src.startswith('-'):
            return None, f'the refspec `{spec}` starts with `-`'
        if src:
            revs.append(f'--glob={src}' if '*' in src else src)
    if revs or refspecs:  # refspecs that were all `:dst` deletions publish nothing
        return revs, ''
    return ['HEAD'], ''


def _attribution_listing(found: list[str]) -> str:
    return '\n'.join(f'  {line}' for line in found)


_REWORD_HINT = (
    'Rewrite those messages: `git commit --amend -F <clean message file>` for HEAD; '
    'for older commits, reword the range (`git filter-branch --msg-filter` over '
    '`<base>..HEAD`, or a non-interactive `git rebase`).'
)


class PushTarget(NamedTuple):
    """One `git push` of a command: the repository and the revisions it
    publishes, or — when either is unknowable before the shell runs — the
    reason, for a gate that has to fail closed."""
    repo: str | None
    revs: list[str] | None
    reason: str


def push_targets(command: str, cwd: str) -> list[PushTarget]:
    """Every `git push` in the runnable `command`, resolved for the gates that
    read what a push publishes (attribution, secrets). Read from the simple
    commands, so a quoted argument that mentions `git push` is not a push; a
    command that does not tokenize and still matches the text pattern is one
    whose target is unknown (the gates fail closed on it)."""
    calls = git_calls(command, cwd)
    if calls is None:
        return ([PushTarget(None, None, 'the command does not parse')]
                if GIT_PUSH_RE.search(command) else [])
    targets: list[PushTarget] = []
    for call in calls:
        if call.sub != 'push':
            continue
        if call.repo is None:
            targets.append(PushTarget(None, None, call.reason))
            continue
        revs, reason = read_push(call.args)
        targets.append(PushTarget(call.repo, revs, reason))
    return targets


def check_push_attribution(command: str, cwd: str) -> dict | None:
    """Deny a push that would publish a commit carrying AI attribution.

    The publication point is the one check every path has to cross: a message
    written to a file by a script, an amend or cherry-pick reusing an old
    message, a commit made outside the session, and the pull request body a
    forge pre-fills from a single commit all reach the remote only through a
    push. So this one fails CLOSED: a push whose target repository or
    revisions cannot be resolved is denied with the reason, because there is
    no later check to catch what it lets through.

    `command` is the runnable command (heredoc bodies and inline scripts
    stripped): a push mentioned inside a script is not a push.

    A dry run is recognized per push, not anywhere in the command, so
    `git push --dry-run && git push` still checks the second one; and
    `--porcelain` is not a dry run here, because it only changes the output
    of a push that still happens."""
    if attribution_allowed():
        return None
    for target in push_targets(command, cwd):
        found = (unpublished_attribution(target.repo, target.revs)
                 if target.repo is not None and target.revs is not None else None)
        if found is not None:
            tags = pushed_tag_attribution(target.repo, target.revs)
            found = None if tags is None else found + tags
        if found:
            return {
                'block': True,
                'msg': (
                    f'[playbook:commit] Push denied — {len(found)} commit(s) or tag(s) this '
                    'push would publish carry AI attribution (a Co-Authored-By trailer '
                    'naming Claude or the Anthropic noreply address, or a "Generated '
                    'with/by Claude" line):\n' + _attribution_listing(found) + '\n'
                    + _REWORD_HINT + ' For a tag, create it again with a clean message '
                    '(`git tag -a -f <name> -F <clean message file> <commit>`). A pull '
                    'request pre-filled from these commits would carry the same lines.'
                ),
            }
        if found is not None:
            continue
        reason = target.reason or 'git could not list the commits it publishes'
        return {
            'block': True,
            'msg': (
                f'[playbook:commit] Push denied — the hook could not determine what this '
                f'push publishes ({reason}), so it cannot confirm that no commit carries '
                'AI attribution. Push with literal arguments from the repository '
                'directory (e.g. `git push origin <branch>`), or ask the user to run '
                'the push themselves.'
            ),
        }
    return None


_REVIEW_LOG_TIMEOUT = 4.0
_OBJECT_NAME_RE = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')


def _in_repository(cwd: str) -> bool | None:
    """Whether `cwd` is inside a git repository; None when git could not say."""
    timeout = _time_left(2)
    if timeout <= 0 or not os.path.isdir(cwd):
        return None
    try:
        done = subprocess.run(('git', '-C', cwd, 'rev-parse', '--git-dir'),
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode == 0


def _existing_objects(repo: str, names: list[str]) -> list[str] | None:
    """The commits and tags among `names` that `repo` holds."""
    names = [name for name in names if _OBJECT_NAME_RE.fullmatch(name)]
    if not names:
        return []
    listing = _git_out(repo, 'cat-file', '--batch-check=%(objectname) %(objecttype)',
                       stdin='\n'.join(names) + '\n', timeout=2)
    if listing is None:
        return None
    return [name for name, _, kind in (line.partition(' ') for line in listing.splitlines())
            if kind in ('commit', 'tag')]


SNAPSHOT_MAX_PATHS = 2000
# Commits checked one by one against what a review read; a longer push is
# credited by HEAD only, so the check stays inside the hook's git budget.
_CONTENT_CHECK_MAX_COMMITS = 50
_NULL_OBJECT_RE = re.compile(r'0{40}(?:0{24})?')
_UNHASHABLE = '?'


class ReviewedTree(NamedTuple):
    """The working tree of a repository as a review read it: the HEAD it sat
    on and the blob of every path it changed against that HEAD ('' for a path
    it deleted, '?' for one hashed differently from how git stores it)."""
    head: str
    blobs: dict[str, str]


class Unreviewed(NamedTuple):
    count: int
    changed_after_review: list[str]


def reads_working_tree(diff_args: list[str]) -> bool:
    """Whether `git diff <diff_args>` prints the working tree: no `--cached`,
    and at most one revision, so the other side is the files on disk. A word
    before `--` that may be a path counts as a revision, so an ambiguous read
    is taken as one of commits only."""
    words = diff_args[:diff_args.index('--')] if '--' in diff_args else diff_args
    if any(word in ('--cached', '--staged', '--no-index') for word in words):
        return False
    revisions = [word for word in words if not word.startswith('-')]
    return len(revisions) <= 1 and not any('..' in word for word in revisions)


def working_tree_blobs(repo: str) -> dict[str, str] | None:
    """Blob of every path the working tree of `repo` (its top-level directory)
    changes against HEAD, untracked files included, hashed without writing to
    the object store. None when git fails or runs out of time, or when more
    than SNAPSHOT_MAX_PATHS paths changed."""
    changed = _git_out(repo, 'diff', '--name-only', '--no-renames', '-z', 'HEAD', timeout=2)
    # Trade-off: `git diff` never prints an untracked file, yet one is credited
    # as read. Leaving them out made every new file of a change reviewed before
    # its first commit a second review; the gate already credits a whole HEAD
    # for a read limited to some paths, and this keeps the same granularity.
    untracked = _git_out(repo, 'ls-files', '--others', '--exclude-standard', '-z', timeout=2)
    if changed is None or untracked is None:
        return None
    paths = list(dict.fromkeys(path for path in (changed + untracked).split('\0') if path))
    if len(paths) > SNAPSHOT_MAX_PATHS:
        return None
    blobs: dict[str, str] = {}
    to_hash: list[str] = []
    for path in paths:
        full = os.path.join(repo, path)
        if not os.path.lexists(full):
            blobs[path] = ''
        elif '\n' in path or os.path.islink(full) or not os.path.isfile(full):
            blobs[path] = _UNHASHABLE
        else:
            to_hash.append(path)
    if to_hash:
        out = _git_out(repo, 'hash-object', '--stdin-paths',
                       stdin='\n'.join(to_hash) + '\n', timeout=3)
        ids = out.split() if out is not None else []
        if len(ids) != len(to_hash):
            return None
        blobs.update(zip(to_hash, ids))
    return blobs


def _commit_changes(repo: str, parent: str, commit: str) -> dict[str, str] | None:
    """Path -> blob the commit leaves there ('' when it deletes the path, '?'
    for a submodule or a symlink)."""
    out = _git_out(repo, 'diff-tree', '-r', '--no-renames', '-z', parent, commit, timeout=2)
    if out is None:
        return None
    fields = out.split('\0')
    changes: dict[str, str] = {}
    for meta, path in zip(fields[0::2], fields[1::2]):
        parts = meta.lstrip(':').split()
        if len(parts) < 5:
            return None
        mode, blob = parts[1], parts[3]
        if _NULL_OBJECT_RE.fullmatch(blob):
            changes[path] = ''
        elif mode in ('160000', '120000'):
            changes[path] = _UNHASHABLE
        else:
            changes[path] = blob
    return changes


def _blobs_at(repo: str, head: str, paths: list[str]) -> dict[str, str] | None:
    """Blob of each path in `head` ('' when `head` does not have it)."""
    asked = [path for path in paths if '\n' not in path]
    out = _git_out(repo, 'cat-file', '--batch-check=%(objectname)',
                   stdin=''.join(f'{head}:{path}\n' for path in asked), timeout=2)
    if out is None:
        return None
    lines = out.splitlines()
    if len(lines) != len(asked):
        return None
    found = {path: ('' if line.endswith(' missing') else line.strip())
             for path, line in zip(asked, lines)}
    return {path: found.get(path, _UNHASHABLE) for path in paths}


def _differs_from_tree(repo: str, changes: dict[str, str], tree: ReviewedTree) -> list[str] | None:
    """Paths the commit leaves with content other than the reviewed tree had.
    A path the tree did not change held what its HEAD holds."""
    unlisted = [path for path in changes if path not in tree.blobs]
    at_head = _blobs_at(repo, tree.head, unlisted) if unlisted else {}
    if at_head is None:
        return None
    differing = []
    for path, blob in changes.items():
        seen = tree.blobs[path] if path in tree.blobs else at_head[path]
        if blob == _UNHASHABLE or seen == _UNHASHABLE or blob != seen:
            differing.append(path)
    return differing


def unreviewed_commits(repo: str, revs: list[str], reviewed: list[str],
                       trees: list[ReviewedTree] | None = None) -> Unreviewed | None:
    """The commits a push of `revs` from `repo` publishes that no
    remote-tracking ref has and no review covers, and the paths whose content
    changed after a review read them.

    A review covers a commit a reviewed HEAD contains, and a commit with one
    parent that is covered and whose every changed path holds what a reviewed
    working tree held: the review read the change before it was committed. A
    merge is covered by a reviewed HEAD only. 0 commits for an empty `revs` (a
    deletion or a dry run) and outside a repository, where nothing is
    published. None when that cannot be established: `repo` does not exist
    yet, git failed or timed out, or the hook has no time left."""
    if not revs:
        return Unreviewed(0, [])
    inside = _in_repository(repo)
    if not inside:
        return None if inside is None else Unreviewed(0, [])
    known = _existing_objects(repo, reviewed)
    if known is None:
        return None
    out = _git_out(repo, 'rev-list', '--reverse', '--parents', *revs, '--not', '--remotes',
                   *known, '--', timeout=_REVIEW_LOG_TIMEOUT)
    if out is None:
        return None
    commits = [line.split() for line in out.splitlines() if line.strip()]
    if not trees or len(commits) > _CONTENT_CHECK_MAX_COMMITS:
        return Unreviewed(len(commits), [])
    pending = {commit[0] for commit in commits}
    covered: set[str] = set()
    changed_after: list[str] = []
    for sha, *parents in commits:
        if len(parents) != 1 or (parents[0] in pending and parents[0] not in covered):
            continue
        changes = _commit_changes(repo, parents[0], sha)
        if changes is None:
            return None
        closest: list[str] | None = None
        for tree in trees:
            differing = _differs_from_tree(repo, changes, tree)
            if differing is None:
                return None
            if closest is None or len(differing) < len(closest):
                closest = differing
            if not differing:
                break
        if closest:
            changed_after += [path for path in closest if path not in changed_after]
        else:
            covered.add(sha)
    return Unreviewed(len(commits) - len(covered), changed_after)


def check_new_commit_attribution(command: str, cwd: str) -> str | None:
    """Message for the model when a command that writes history left a commit
    carrying AI attribution on HEAD that no remote has yet.

    Runs after the tool, so it cannot stop the commit; it makes sure the commit
    is fixed now rather than discovered at push time, after more work is
    stacked on it. It reads the history, not the command, so it catches a
    message however it was assembled. Advisory, so it fails open: what it
    cannot resolve is left to `check_push_attribution`."""
    if attribution_allowed():
        return None
    runnable = strip_inline_payloads(strip_heredoc_bodies(command, PUBLISH_TOOLS))
    checked: set[str] = set()
    for call in git_calls(runnable, cwd) or ():
        repo = call.repo
        if call.sub not in _HISTORY_WRITE_SUBS or repo is None or repo in checked:
            continue
        checked.add(repo)
        found = unpublished_attribution(repo, ['HEAD'])
        if found:
            return (
                '[playbook:commit] Local history now carries AI attribution on commits '
                'no remote has yet (a Co-Authored-By trailer naming Claude or the '
                'Anthropic noreply address, or a "Generated with/by Claude" line):\n'
                + _attribution_listing(found) + '\n'
                + _REWORD_HINT + ' Fix it before anything else: the push gate denies '
                'publishing these commits.'
            )
    return None


# ─── Secrets ──────────────────────────────────────────────────────────────────
#
# Three check points, each owning a different failure mode:
#   - a write (editing tool or shell) — denies before the value reaches disk;
#   - a commit — reads the staged diff; fails OPEN, the push is the backstop;
#   - a push — reads every commit no remote has; fails CLOSED, nothing after it.
# Only the deny tier blocks. The warn tier (keyword-anchored rules, often a
# placeholder) is surfaced to the model on a write and ignored elsewhere.

SECRET_WRITE_TOOLS = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'ApplyPatch')

_SECRETS_LISTED = 10
_SECRET_DIFF_TIMEOUT = 3.0
_SECRET_LOG_TIMEOUT = 4.0

_SECRET_FIX = (
    'Read the value from configuration at run time instead (an environment variable, '
    'a git-ignored `.env`, a secret manager). If it is not a real secret (a test '
    'fixture, a documented example), only the user can allow it, by adding the '
    f'fingerprint line to `{secret_scan.ALLOW_FILE}`. You cannot write that file, and '
    'inline markers such as `gitleaks:allow` are not honored.'
)

# Short options of `git commit` whose value may follow in the same cluster
# (`-mfix`): an `a` after one of them is part of the value, not `--all`.
_COMMIT_VALUE_SHORT = frozenset('mFCcSt')

# Writes other than `sed -i`, which `_runs_sed_in_place` reads per command.
# `>&2` and `2>&1` duplicate a descriptor; they write no file.
_BASH_WRITE_RE = re.compile(r'>(?!>?&)|\btee\b|<<')
_BASH_REDIRECT_TARGET_RE = re.compile(
    r"""(?:(?<![<>&])\d*>>?(?!&)|\btee\s+(?:-a\s+)?)\s*(["']?)([^\s;&|<>"']+)\1""")
# Shell forms that change a file. Reading the allow file is fine (the Read tool
# exists for that); changing it is the user's alone.
_MUTATES_FILE_RE = re.compile(
    r'>|\b(?:tee|sed|cp|mv|ln|rm|dd|install|truncate|python3?|node|perl|ruby|awk|echo|printf)\b')


def _repo_root(path: str) -> str | None:
    """Top level of the repository holding `path` (which may not exist yet)."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    out = _git_out(probe, 'rev-parse', '--show-toplevel', timeout=2) if probe else None
    return out.strip() or None if out else None


def _is_allow_file(abs_path: str) -> bool:
    return abs_path.replace(os.sep, '/').endswith('/' + secret_scan.ALLOW_FILE.replace(os.sep, '/'))


def _secret_scope(abs_path: str) -> tuple[str, str] | None:
    """(repository root, path relative to it) when a write to `abs_path` lands
    where git would track it. None outside a repository, inside `.git`, and
    for a git-ignored path — a secret in an ignored `.env` is the practice
    the deny message recommends, not a leak."""
    root = _repo_root(os.path.dirname(abs_path))
    if root is None:
        return None
    rel = os.path.relpath(abs_path, root)
    if rel.startswith(os.pardir) or rel.split(os.sep, 1)[0] == '.git':
        return None
    try:
        ignored = subprocess.run(('git', '-C', root, 'check-ignore', '-q', '--', rel),
                                 capture_output=True, timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ignored = False  # unknown: scan it
    return None if ignored else (root, rel)


def _resolve(path: str, base: str) -> str:
    # realpath: git reports the resolved top level, so a path reached through a
    # symlink would otherwise sit "outside" its own repository and go unscanned.
    return os.path.realpath(os.path.join(base or os.getcwd(), os.path.expanduser(path)))


def _read_existing(abs_path: str) -> str:
    try:
        if os.path.getsize(abs_path) > secret_scan.MAX_SCAN_CHARS:
            return ''
        with open(abs_path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return ''


def _written_text(tool_name: str, tool_input: dict, abs_path: str) -> str:
    """What an editing call adds to the file (see secret_scan.added_lines)."""
    if tool_name == 'Write':
        return secret_scan.added_lines(_read_existing(abs_path), str(tool_input.get('content') or ''))
    if tool_name == 'Edit':
        return secret_scan.added_lines(str(tool_input.get('old_string') or ''),
                                       str(tool_input.get('new_string') or ''))
    if tool_name == 'MultiEdit':
        edits = tool_input.get('edits')
        return '\n'.join(
            secret_scan.added_lines(str(e.get('old_string') or ''), str(e.get('new_string') or ''))
            for e in (edits if isinstance(edits, list) else []) if isinstance(e, dict))
    if tool_name == 'NotebookEdit':
        return str(tool_input.get('new_source') or '')
    return ''


def _secret_listing(found: list['secret_scan.Finding']) -> str:
    lines = []
    for f in found[:_SECRETS_LISTED]:
        where = f'{f.commit} ' if f.commit else ''
        lines.append(f'  - {where}{f.path or "(command)"}: {f.rule_id} {f.redacted} '
                     f'— fingerprint `{f.fingerprint}`')
    if len(found) > _SECRETS_LISTED:
        lines.append(f'  - ... and {len(found) - _SECRETS_LISTED} more')
    return '\n'.join(lines)


def _secret_verdict(found: list['secret_scan.Finding'], repo_root: str | None,
                    denied_what: str, warn: bool = False) -> dict | None:
    """Block on deny-tier findings the user has not allowed; with `warn`, tell
    the model about warn-tier ones. The message never carries the value."""
    allowed = secret_scan.load_allowed(repo_root) if repo_root else frozenset()
    found = [f for f in found if f.fingerprint not in allowed]
    denied = [f for f in found if f.tier == secret_scan.TIER_DENY]
    if denied:
        return {
            'block': True,
            'msg': (f'[playbook:secrets] {denied_what} — {len(denied)} secret(s):\n'
                    + _secret_listing(denied) + '\n' + _SECRET_FIX),
        }
    possible = [f for f in found if f.tier == secret_scan.TIER_WARN]
    if warn and possible:
        return {
            'block': False,
            'to_model': True,
            'msg': ('[playbook:secrets] Possible secret(s) — a keyword-anchored rule, often '
                    'a placeholder:\n' + _secret_listing(possible) + '\nIf a value is '
                    'real, do not keep it in the file. ' + _SECRET_FIX),
        }
    return None


_ALLOW_FILE_MSG = (
    f'[playbook:secrets] `{secret_scan.ALLOW_FILE}` holds the user\'s exceptions to the '
    'secret scanner, so only the user changes it. Show them the fingerprint line(s) '
    'and ask them to add it.'
)


_PATCH_FILE_RE = re.compile(r'^\*\*\* (?:Add|Update) File: (.+)$')


def _patch_additions(tool_input: dict) -> list[tuple[str, str]]:
    """(path, added text) per file of a Codex apply_patch call. The patch is
    whichever string field holds `*** Begin Patch`: the payload's field name
    has not been confirmed against a live Codex session."""
    patch = next((v for v in tool_input.values()
                  if isinstance(v, str) and '*** Begin Patch' in v), '')
    files: list[tuple[str, list[str]]] = []
    for line in patch.splitlines():
        header = _PATCH_FILE_RE.match(line)
        if header:
            files.append((header.group(1).strip(), []))
        elif line.startswith('*** '):
            continue
        elif files and line.startswith('+'):
            files[-1][1].append(line[1:])
    return [(path, '\n'.join(lines)) for path, lines in files]


def check_secret_write(tool_name: str, tool_input: dict, cwd: str = '') -> dict | None:
    """Deny an editing call that adds a secret to a file git would track."""
    if tool_name not in SECRET_WRITE_TOOLS:
        return None
    if tool_name == 'ApplyPatch':
        for path, added in _patch_additions(tool_input):
            verdict = _check_written(_resolve(path, cwd), added)
            if verdict:
                return verdict
        return None
    path = str(tool_input.get('file_path') or tool_input.get('notebook_path') or '')
    if not path:
        return None
    abs_path = _resolve(path, cwd)
    return _check_written(abs_path, _written_text(tool_name, tool_input, abs_path))


def _check_written(abs_path: str, text: str) -> dict | None:
    if _is_allow_file(abs_path):
        return {'block': True, 'msg': _ALLOW_FILE_MSG}
    # Scan first, ask git second: nearly every write is clean, and the scan is
    # in-process while resolving the repository costs two git subprocesses.
    # The pre-check names the file only, never its absolute path: gitleaks'
    # path allowlists are unanchored, and `/Users/u/node_modules/proj/a.py`
    # matched the vendored-code entry, turning the gate off for a whole
    # checkout. The repository-relative scan below stays authoritative.
    if not secret_scan.scan_text(text, os.path.basename(abs_path)):
        return None  # None (too large, or out of time) is the commit's to catch
    scope = _secret_scope(abs_path)
    if scope is None:
        return None
    root, rel = scope
    # Again with the repository-relative path, which gitleaks' path allowlists expect.
    found = secret_scan.scan_text(text, rel)
    if not found:
        return None
    return _secret_verdict(found, root, f'Write to `{rel}` denied', warn=True)


def check_secret_bash(command: str, cwd: str = '') -> dict | None:
    """Deny a shell command that writes a secret into a tracked file, or that
    changes the user's allow file.

    Reads the RAW command: a heredoc body is exactly what lands in the file.
    The value is looked for in the whole command when any write target is
    tracked or cannot be resolved; a command whose every target is ignored or
    outside a repository is left alone."""
    if not command:
        return None
    # The allow file is looked for outside heredoc bodies: a commit message or
    # a doc that NAMES the file is data. A heredoc writing INTO it still names
    # it as the redirect target, which stays. Inline `-c` payloads are kept —
    # an interpreter one-liner opening the file is exactly a write to it.
    shell = strip_heredoc_bodies(command)
    if (secret_scan.ALLOW_FILE.replace(os.sep, '/') in shell.replace('\\', '/')
            and _MUTATES_FILE_RE.search(shell)):
        return {'block': True, 'msg': _ALLOW_FILE_MSG}
    edits_in_place = _runs_sed_in_place(command)
    if not (edits_in_place or _BASH_WRITE_RE.search(command)):
        return None
    found = secret_scan.scan_text(command, tiers=(secret_scan.TIER_DENY,))
    if not found:
        return None
    root = _command_root(command, cwd) or cwd or os.getcwd()
    targets = [t for _, t in _BASH_REDIRECT_TARGET_RE.findall(command)]
    # Every file the command writes is known and none is tracked (`/dev/null`,
    # an ignored `.env`, a path outside the repository): nothing is leaked.
    # An in-place `sed` writes a file the redirect list cannot see.
    if (targets and not edits_in_place
            and all(t.startswith('/dev/') or ('$' not in t and _secret_scope(_resolve(t, root)) is None)
                    for t in targets)):
        return None
    return _secret_verdict(found, _repo_root(root), 'Command denied: it writes')


def _commit_stages_all(args: list[str]) -> bool:
    for arg in args:
        if arg in ('-a', '--all'):
            return True
        if arg.startswith('-') and not arg.startswith('--'):
            for flag in arg[1:]:
                if flag == 'a':
                    return True
                if flag in _COMMIT_VALUE_SHORT:
                    break
    return False


def check_commit_secrets(command: str, cwd: str = '') -> dict | None:
    """Deny a `git commit` whose recorded changes add a secret.

    Reads the staged diff, or the whole diff against HEAD when the commit
    stages tracked files itself (`-a`) or a `git add` runs earlier in the same
    command. New untracked files added in that same command are not visible
    yet; the push check reads the commit itself. Fails open for the same
    reason: an unresolvable directory or a git error is the push check's to
    catch. `command` is the runnable command."""
    added = False
    for call in git_calls(command, cwd) or ():
        if call.sub == 'add':
            added = True
        if call.sub != 'commit' or call.repo is None:
            continue
        repo = call.repo
        whole = _commit_stages_all(call.args) or added
        diff = ('-U0', '--no-color', '--no-ext-diff')
        patch = (_git_out(repo, 'diff', 'HEAD', *diff, timeout=_SECRET_DIFF_TIMEOUT)
                 if whole else None)
        if patch is None:  # also the first commit, which has no HEAD to diff against
            patch = _git_out(repo, 'diff', '--cached', *diff, timeout=_SECRET_DIFF_TIMEOUT)
        if patch is None:
            continue
        found = secret_scan.scan_patch(patch, tiers=(secret_scan.TIER_DENY,))
        verdict = _secret_verdict(found or [], _repo_root(repo), 'Commit denied: it records')
        if verdict:
            return verdict
    return None


def _push_secrets_unknown(reason: str) -> dict:
    return {
        'block': True,
        'msg': (
            f'[playbook:secrets] Push denied — the hook could not read what this push '
            f'publishes ({reason}), so it cannot confirm that no commit carries a secret. '
            'Push with literal arguments from the repository directory (e.g. `git push '
            'origin <branch>`), push a smaller range, or ask the user to run the push.'
        ),
    }


def check_push_secrets(command: str, cwd: str = '') -> dict | None:
    """Deny a push that would publish a commit adding a secret.

    Every route into history (an amend, a cherry-pick, a commit made outside
    the session, an untracked file added in the same command as the commit)
    reaches the remote only through here, so this one fails CLOSED. `command`
    is the runnable command."""
    targets = push_targets(command, cwd)
    if targets and not secret_scan.rules_loaded():
        return _push_secrets_unknown('the secret rules file is missing or corrupt')
    for target in targets:
        if target.repo is None or target.revs is None:
            return _push_secrets_unknown(target.reason)
        if not target.revs or _git_out(target.repo, 'rev-parse', '--git-dir', timeout=2) is None:
            continue  # a deletion or a dry run publishes nothing; no repository, nothing to publish
        if _time_left(_SECRET_LOG_TIMEOUT) <= 0:
            return _push_secrets_unknown('the hook ran out of time to read its commits')
        try:
            done = subprocess.run(
                ('git', '-C', target.repo, 'log', '-p', '-U0', '--no-color', '--no-ext-diff',
                 '--format=commit %h', *target.revs, '--not', '--remotes', '--'),
                capture_output=True, text=True, errors='replace',
                timeout=_time_left(_SECRET_LOG_TIMEOUT))
        except subprocess.TimeoutExpired:
            return _push_secrets_unknown('reading its commits timed out')
        except (OSError, subprocess.SubprocessError):
            return _push_secrets_unknown('git could not list the commits it publishes')
        if done.returncode != 0:
            return _push_secrets_unknown('git could not list the commits it publishes')
        found = secret_scan.scan_patch(done.stdout, tiers=(secret_scan.TIER_DENY,))
        if found is None:
            return _push_secrets_unknown('its commits are too large or too slow to scan here')
        verdict = _secret_verdict(found, _repo_root(target.repo),
                                  'Push denied: commits it would publish add')
        if verdict:
            return verdict
    return None


def project_key(cwd: str) -> str:
    """Stable name for a project's state files: the same directory, reached by
    any path or symlink, gets the same key."""
    return hashlib.sha256(os.path.realpath(cwd).encode()).hexdigest()[:16]


def state_path(state_dir: str, session_id: str = '', cwd: str | None = None,
               agent_id: str = '') -> str:
    """Session-scoped state file path under state_dir. Falls back to a slug
    derived from cwd when no session_id is available.

    A subagent's hook events carry its parent's session_id in both Claude Code
    and Codex (captured live); only agent_id tells them apart. Keyed by
    session_id alone, a subagent's code write armed — and its validation
    command cleared — the parent session's Stop gates."""
    key = '-'.join(filter(None, (session_id, agent_id)))
    if key:
        name = f'.state-{key}.json'
    else:
        slug = (cwd or os.getcwd()).lstrip('/').replace('/', '-')
        name = f'.state-{slug}.json'
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, name)


def subagent_state_paths(state_dir: str, session_id: str) -> list[str]:
    """The state files state_path gave this session's subagents."""
    if not session_id:
        return []
    return glob.glob(os.path.join(state_dir, f'.state-{glob.escape(session_id)}-*.json'))


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    """Write the state file atomically.

    A plain open('w') truncates before it writes: a crash or a full disk in that
    window leaves a half-written file, load_state fails open to {}, and every
    gate in the session silently resets. Write to a sibling temp file and rename
    — rename is atomic on POSIX, so a reader sees either the old state or the
    new one, never a partial one."""
    directory = os.path.dirname(path) or '.'
    handle, tmp = tempfile.mkstemp(dir=directory, prefix='.state-tmp-')
    try:
        with os.fdopen(handle, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(tmp)
        raise


# ─── Learn loop (failure capture) ─────────────────────────────────────────────

LEARNED_TOKEN = '[LEARNED]'

# User messages that say the previous turn was wrong, or was built on a wrong
# premise. Deliberately phrase-level, not keyword-level: a bare "errado" or
# "wrong" appears constantly in normal technical discussion (asking what happens
# when a value is wrong, naming a test case), and every false positive here
# costs a blocked turn end. Each alternative below only matches a user actually
# rejecting what was just done.
CORRECTION_RE = re.compile(
    r'\bn[ãa]o\s+(?:é|e|era|foi)\s+(?:isso|isto|bem\s+isso)\b'
    r'|\b(?:t[áa]|est[áa]|ficou|ficaram|continua)\s+errad[oa]s?\b'
    r'|\b(?:voc[êe]|vc)\s+(?:errou|assumiu|inventou|quebrou|entendeu\s+errado)\b'
    r'|\bn[ãa]o\s+(?:foi|é|e)\s+(?:o\s+)?que\s+(?:eu\s+)?(?:pedi|falei|disse)\b'
    r'|\b(?:reverte|reverta|desfaz|desfa[çc]a)\b'
    r'|\bpremissa\s+errada\b'
    r'|\bcontinua\s+(?:errado|quebrado|com\s+(?:erro|bug))\b'
    r"|\b(?:that'?s|that\s+is|this\s+is)\s+(?:wrong|incorrect|not\s+right)\b"
    r'|\bnot\s+what\s+i\s+(?:asked|wanted|said)\b'
    r'|\byou\s+(?:broke|assumed|misunderstood|hallucinated)\b'
    r'|\b(?:revert|undo)\s+(?:that|it|this)\b'
    r'|\bstill\s+(?:broken|failing|wrong)\b'
    r"|\b(?:doesn'?t|does\s+not|didn'?t|did\s+not)\s+work\b"
    r'|\bwrong\s+(?:assumption|premise|approach|file|path)\b',
    re.IGNORECASE,
)

# Text that reaches a prompt hook or the transcript as a "user" message without
# the user having typed it: harness reminders, background-task notifications,
# local command output, and the report a subagent hands back.
NON_PROMPT_RE = re.compile(
    r'^\s*(?:<system-reminder|<task-notification|<local-command|<agent-message|\[SYSTEM NOTIFICATION)'
)
# A block the user pasted from somewhere else. An unclosed block runs to the end.
_PASTED_BLOCK_RE = re.compile(r'<pasted_content\b[^>]*>.*?(?:</pasted_content[^>]*>|\Z)', re.DOTALL)


def typed_words(prompt: str) -> str:
    """What the user wrote themselves in `prompt`: '' for a message they did
    not write at all, and the message without its pasted blocks otherwise.

    A correction is something the user says. A subagent report that quotes
    "that's wrong", or a pasted document that contains "reverte", says nothing
    about the previous turn (measured: 34 of 59 journaled corrections were
    subagent reports or notifications)."""
    if NON_PROMPT_RE.match(prompt):
        return ''
    return _PASTED_BLOCK_RE.sub(' ', prompt)


# ─── Consent in the chat (auto mode) ─────────────────────────────────────────
#
# In auto mode a destructive command is denied without a dialog and queued
# here, per session; the model goes on with the rest of the work and asks the
# user in its message. The user's own words, read from the UserPromptSubmit
# payload (never the transcript), approve it, and the model then runs the
# exact command once.

CONSENT_PENDING_MAX = 20
_CONSENT_MAX_AGE_S = 7 * 24 * 3600
_CONSENT_COMMAND_KEPT = 2000

_DELETE_INFINITIVE = r'(?:apagar|remover|deletar|excluir|tirar|limpar|borrar|eliminar|quitar)'
_DELETE_IMPERATIVE = (r'(?:apag(?:a|ue)|remov[ae]|delet[ae]|exclu[ai]|tir[ae]|limp[ae]'
                      r'|borr[ae]|elimin[ae]|quit[ae])')
_DELETE_EN = r'(?:delete|remove|erase|wipe|drop)'
_OBJECT_WORD = (r'(?:isso|isto|ele|ela|eles|elas|ess[ea]s?|est[ea]s?|aquilo|tudo|os\s+dois|as\s+duas'
                r'|tod[oa]s|it|them|that|those|this|these|both|all|eso|esto|todo)')
# A consent names the deletion: "pode apagar", "apaga isso", "yes, delete it".
# A bare verb is not one: "remova o import" while a removal is queued is an
# instruction about code, not an answer to the queue.
CONSENT_RE = re.compile(
    rf'\bpode[ms]?\s+(?:sim\s+)?(?:j[áa]\s+)?{_DELETE_INFINITIVE}\b'
    rf'|\bpuedes?\s+(?:s[ií]\s+)?{_DELETE_INFINITIVE}\b'
    rf'|\b{_DELETE_IMPERATIVE}\s+{_OBJECT_WORD}\b'
    rf'|\bsim[,!.]?\s+(?:pode\s+)?{_DELETE_IMPERATIVE}\b'
    rf'|\b(?:you\s+)?(?:can|may)\s+{_DELETE_EN}\b'
    rf'|\byes[,!.]?\s+(?:please\s+)?{_DELETE_EN}\b'
    rf'|\b{_DELETE_EN}\s+{_OBJECT_WORD}\b',
    re.IGNORECASE)
# A negation just before the consent words turns them into a refusal.
_CONSENT_NEGATION_RE = re.compile(
    r"\b(?:n[ãa]o|nem|nunca|jamais|no|not|never|don'?t|do\s+not)\b[^.!?\n]{0,25}$",
    re.IGNORECASE)
_CONSENT_ID_RE = re.compile(r'#(\d{1,4})\b')


class ConsentReading(NamedTuple):
    approves: bool
    refuses: bool
    ids: frozenset[int]


def read_consent(text: str) -> ConsentReading:
    """What the user's own words say about the queued removals: an approval,
    a refusal (a negated consent), both, or neither; and the `#n` numbers they
    name, which narrow an approval to those."""
    approves = refuses = False
    for match in CONSENT_RE.finditer(text):
        if text[match.end():match.end() + 2].lstrip().startswith('?'):
            continue  # "pode apagar?" asks, it does not answer
        if _CONSENT_NEGATION_RE.search(text[:match.start()]):
            refuses = True
        else:
            approves = True
    return ConsentReading(approves, refuses,
                          frozenset(int(n) for n in _CONSENT_ID_RE.findall(text)))


_CONSENT_FILE_PREFIX = '.consent-'
COMMAND_SHOWN_CHARS = 300


def shown_command(command: str) -> str:
    """A command as the user is shown it: cut, with an ellipsis, past
    COMMAND_SHOWN_CHARS."""
    if len(command) <= COMMAND_SHOWN_CHARS:
        return command
    return command[:COMMAND_SHOWN_CHARS] + '…'


def consent_path(state_dir: str, session_id: str) -> str:
    return os.path.join(state_dir, f'{_CONSENT_FILE_PREFIX}{session_id}.json')


_TEMPORARY_DATA_PART = '/temporary-data/'


def touches_consent_queue(tool_name: str, tool_input: dict) -> bool:
    """Whether a tool call names the consent queue's files or the Temporary
    data folder. The queue is the user's answer, and each Temporary data item
    is a command the user's press runs: the model writing either could approve
    itself or plant one. A path the shell computes (`.con"sent"-`) is not
    seen: a known gap."""
    if tool_name == 'Bash':
        command = str(tool_input.get('command') or '')
        return _CONSENT_FILE_PREFIX in command or _TEMPORARY_DATA_PART in command
    path = str(tool_input.get('file_path') or tool_input.get('notebook_path') or '')
    return (os.path.basename(path).startswith(_CONSENT_FILE_PREFIX)
            or _TEMPORARY_DATA_PART in path.replace('\\', '/'))


@contextlib.contextmanager
def _file_lock(path: str):
    """An exclusive lock on `path`.lock where the platform has flock: the gate
    queuing a command and the prompt hook approving it may run at once."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(path + '.lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _command_key(command: str) -> str:
    return hashlib.sha256(command.encode('utf-8', 'replace')).hexdigest()


def _prune_consent_files(state_dir: str) -> None:
    cutoff = time.time() - _CONSENT_MAX_AGE_S
    for path in glob.glob(os.path.join(glob.escape(state_dir), f'{_CONSENT_FILE_PREFIX}*.json')):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                if os.path.exists(path + '.lock'):
                    os.remove(path + '.lock')
        except OSError:
            continue


def _consent_entries(data: dict, key: str) -> list[dict]:
    entries = data.get(key)
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def consent_request(state_dir: str, session_id: str, command: str, effect: str,
                    cwd: str = '') -> int:
    """Queue `command` for the user's approval and return its number; the same
    command queued again keeps its number. `cwd` is where the model's shell
    was: the button runs the command there."""
    os.makedirs(state_dir, exist_ok=True)
    _prune_consent_files(state_dir)
    path = consent_path(state_dir, session_id)
    key = _command_key(command)
    with _file_lock(path):
        data = load_state(path)
        pending = _consent_entries(data, 'pending')
        for entry in pending:
            if entry.get('key') == key:
                return _consent_number(entry)
        next_id = data.get('next_id')
        number = next_id if isinstance(next_id, int) and next_id > 0 else 1
        # A cut command is kept for showing; the button never runs it (`is_cut`).
        pending.append({'id': number, 'key': key, 'command': command[:_CONSENT_COMMAND_KEPT],
                        'is_cut': len(command) > _CONSENT_COMMAND_KEPT,
                        'effect': effect, 'cwd': cwd, 'ts': time.time()})
        data['pending'] = pending[-CONSENT_PENDING_MAX:]
        data['next_id'] = number + 1
        save_state(path, data)
    return number


def consent_take(state_dir: str, session_id: str, command: str, use: bool = True) -> bool:
    """Whether the user approved `command` in the chat. With `use`, the
    approval is used up by this call, so the command runs once; without it
    the approval is only looked at, for a caller that may still deny."""
    path = consent_path(state_dir, session_id)
    if not os.path.exists(path):
        return False
    key = _command_key(command)
    with _file_lock(path):
        data = load_state(path)
        approved = _consent_entries(data, 'approved')
        if not any(entry.get('key') == key for entry in approved):
            return False
        if use:
            data['approved'] = [entry for entry in approved if entry.get('key') != key]
            save_state(path, data)
    return True


def consent_mark_turn_end(state_dir: str, session_id: str) -> None:
    """Record that a turn of the main session ended: what was queued before it
    is what the model could have listed to the user in that turn's messages,
    and what a command of it made is released to Temporary data."""
    os.makedirs(state_dir, exist_ok=True)
    path = consent_path(state_dir, session_id)
    with _file_lock(path):
        data = load_state(path)
        data['turn_end'] = time.time()
        save_state(path, data)


def _consent_number(entry: dict) -> int:
    value = entry.get('id')
    return value if isinstance(value, int) else 0


class ConsentAnswer(NamedTuple):
    approved: list[dict]
    still_pending: list[dict]
    ambiguous: bool


def consent_answer(state_dir: str, session_id: str, text: str) -> ConsentAnswer:
    """Apply what the user wrote (`text`, their own words) to the queue: an
    approval moves the queued commands it covers to approved — all of those
    queued before the last turn ended (the ones the model could have shown the
    user), or the `#n` named among them. A message that both approves and
    refuses, or names a number not among them, approves nothing."""
    path = consent_path(state_dir, session_id)
    if not text or not os.path.exists(path):
        return ConsentAnswer([], [], False)
    reading = read_consent(text)
    if not reading.approves:
        return ConsentAnswer([], [], False)
    with _file_lock(path):
        data = load_state(path)
        pending = _consent_entries(data, 'pending')
        if not pending:
            return ConsentAnswer([], [], False)
        turn_end = data.get('turn_end')
        shown = [entry for entry in pending if isinstance(turn_end, (int, float))
                 and isinstance(entry.get('ts'), (int, float)) and entry['ts'] <= turn_end]
        if reading.refuses or not shown:
            return ConsentAnswer([], pending, True)
        chosen = [entry for entry in shown
                  if not reading.ids or _consent_number(entry) in reading.ids]
        if not chosen or (reading.ids - {_consent_number(entry) for entry in shown}):
            return ConsentAnswer([], pending, True)
        chosen_ids = {entry.get('id') for entry in chosen}
        data['pending'] = [entry for entry in pending if entry.get('id') not in chosen_ids]
        data['approved'] = (_consent_entries(data, 'approved') + chosen)[-CONSENT_PENDING_MAX:]
        save_state(path, data)
    return ConsentAnswer(chosen, data['pending'], False)


# ─── The answer from the button above the chat, and Temporary data ──────────
#
# The playbook's mod (hooks/temporary_data.tsx) shows each queued command with
# Approve and Decline; scripts/temporary_data.py carries the answer out here,
# under the queue's lock. A declined deletion is kept in Temporary data, one
# file per item and one folder per project, so each writer creates its own
# file and nothing is rewritten under another writer. Only a deletion is kept:
# a declined reset or force push has no later moment where it would make sense.

DELETION_EFFECTS = frozenset({'delete_tree', 'mass_delete', 'script_delete', 'worktree_remove'})
TEMPORARY_DATA_DIR = 'temporary-data'
TEMPORARY_DATA_MAX = 200
_ITEM_ID_RE = re.compile(r'[0-9a-f]{12}')


def consent_turn_end(state_dir: str, session_id: str) -> float:
    """When the last turn of `session_id` ended; 0 before the first."""
    value = load_state(consent_path(state_dir, session_id)).get('turn_end')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def consent_pending(state_dir: str, session_id: str) -> list[dict]:
    """The commands of `session_id` still waiting for the user's answer."""
    return _consent_entries(load_state(consent_path(state_dir, session_id)), 'pending')


def consent_decide(state_dir: str, session_id: str, number: int) -> dict | None:
    """Take `#number` out of the queue for the answer the user gave on the
    button; the entry, or None when it is no longer waiting (answered in the
    chat, or already by another press)."""
    path = consent_path(state_dir, session_id)
    if not os.path.exists(path):
        return None
    with _file_lock(path):
        data = load_state(path)
        pending = _consent_entries(data, 'pending')
        entry = next((one for one in pending if _consent_number(one) == number), None)
        if entry is None:
            return None
        data['pending'] = [one for one in pending if one is not entry]
        save_state(path, data)
    return entry


def temporary_data_dir(state_dir: str, project_dir: str) -> str:
    """The folder of one project's Temporary data: the project's real path,
    every character but letters and digits as `-`, as Claude Code names its
    own per-project folders."""
    key = re.sub(r'[^A-Za-z0-9]', '-', os.path.realpath(project_dir))
    return os.path.join(state_dir, TEMPORARY_DATA_DIR, key)


TEMPORARY_KINDS = frozenset({'declined', 'created', 'registered'})


def temporary_data_add(state_dir: str, project_dir: str, *, command: str, cwd: str,
                       effect: str, origin: str, kind: str = 'declined',
                       what: dict | None = None, target: dict | None = None) -> dict:
    """Keep a deletion the user has not run yet; the item written.

    `kind`: `declined` on the button, `created` by a command the hook read
    (`created_temporaries`), `registered` by the model. `what` names it for
    the person (`{key, name}` for a playbook_i18n text, `{text}` as written);
    `target` is what the deletion removes (`{kind: path | container | volume
    | network, value}`), so an item whose target is gone leaves the panel."""
    if kind not in TEMPORARY_KINDS:
        raise ValueError(f'not a Temporary data kind: {kind!r}')
    folder = temporary_data_dir(state_dir, project_dir)
    os.makedirs(folder, exist_ok=True)
    item = {'id': os.urandom(6).hex(), 'command': command, 'cwd': cwd, 'effect': effect,
            'origin': origin, 'ts': time.time(), 'kind': kind, 'what': what or {},
            'target': target or {}}
    save_state(os.path.join(folder, f"{item['id']}.json"), item)
    return item


class Temporary(NamedTuple):
    """Something a command made that outlives it: what it is (a playbook_i18n
    key and the name), the command that removes it, and what to check for
    whether it still exists."""
    what_key: str
    name: str
    command: str
    cwd: str
    target_kind: str
    target: str


_DOCKER_CREATES = {('volume', 'create'): 'volume', ('network', 'create'): 'network'}


def _docker_created(words: list[str], cwd: str) -> Temporary | None:
    """A named container `docker run --name` starts without `--rm`, or a
    volume or network `docker volume|network create <name>` makes."""
    args = words[1:]
    if args[:1] == ['run'] or args[:2] == ['container', 'run']:
        if '--rm' in args:
            return None
        name = ''
        for at, word in enumerate(args):
            if word == '--name' and at + 1 < len(args):
                name = args[at + 1]
            elif word.startswith('--name='):
                name = word.split('=', 1)[1]
        if not _is_literal_path(name) or not name:
            return None
        return Temporary('tmp_what_container', name, f'docker rm -f {shlex.quote(name)}', cwd,
                         'container', name)
    kind = _DOCKER_CREATES.get(tuple(args[:2]))
    if kind is None:
        return None
    names = [word for word in args[2:] if not word.startswith('-')]
    if len(names) != 1 or not _is_literal_path(names[0]):
        return None
    return Temporary(f'tmp_what_{kind}', names[0], f'docker {kind} rm {shlex.quote(names[0])}',
                     cwd, kind, names[0])


def _mktemp_created(words: list[str], stdout: str, cwd: str) -> Temporary | None:
    """What `mktemp` made, read from its output: only when the command was
    `mktemp` alone, so the output is its path and nothing else."""
    path = stdout.strip()
    if '\n' in path or not os.path.isabs(path):
        return None
    is_dir = any(word in ('-d', '--directory') or re.fullmatch(r'-[a-zA-Z]*d[a-zA-Z]*', word)
                 for word in words[1:] if word.startswith('-'))
    return Temporary('tmp_what_tempdir' if is_dir else 'tmp_what_tempfile', path,
                     f"rm -{'rf' if is_dir else 'f'} {shlex.quote(path)}", cwd, 'path', path)


def _worktree_created(call: GitCall) -> Temporary | None:
    """The worktree `git worktree add <path>` made, in a repository the
    command names literally."""
    if call.repo is None or call.sub != 'worktree' or call.args[:1] != ['add']:
        return None
    operands: list[str] = []
    skip = False
    for word in call.args[1:]:
        if skip:
            skip = False
        elif word in ('-b', '-B', '--reason'):
            skip = True
        elif not word.startswith('-'):
            operands.append(word)
    if not operands or not _is_literal_path(operands[0]):
        return None
    path = os.path.normpath(os.path.join(call.repo, os.path.expanduser(operands[0])))
    return Temporary('tmp_what_worktree', path,
                     f'git worktree remove --force {shlex.quote(path)}', call.repo, 'path', path)


def created_temporaries(command: str, stdout: str, cwd: str, disposable_root: str = ''
                        ) -> list[Temporary]:
    """What a command that ran made and leaves behind, read from its words:
    a git worktree, a named Docker container, volume or network, and what a
    lone `mktemp` printed. Anything inside `disposable_root` (the session's
    scratchpad) is left out: the session's own clean-up removes it. What the
    words do not name literally (a path from a variable, `$(mktemp -d)`) is
    not read: the model registers it (scripts/temporary_data.py register)."""
    read = _simple_commands(strip_heredoc_bodies(command))
    if read is None:
        return []
    commands = read[0]
    found: list[Temporary] = []
    for words in commands:
        base = words[0].rsplit('/', 1)[-1]
        if base == 'docker':
            made = _docker_created(words, cwd)
        elif base == 'mktemp' and len(commands) == 1:
            made = _mktemp_created(words, stdout, cwd)
        else:
            made = None
        if made is not None:
            found.append(made)
    found += [made for made in map(_worktree_created, git_calls(command, cwd) or [])
              if made is not None]
    if disposable_root:
        root = os.path.realpath(disposable_root)
        found = [made for made in found if made.target_kind != 'path'
                 or os.path.commonpath((os.path.realpath(made.target), root)) != root]
    return found


def temporary_data_note_created(state_dir: str, project_dir: str, made: list[Temporary],
                                origin: str) -> int:
    """Keep what a command made in Temporary data, once per target; how many
    were added."""
    if not made:
        return 0
    known = {(item['target'].get('kind'), item['target'].get('value'))
             for item in temporary_data_items(state_dir, project_dir)
             if isinstance(item.get('target'), dict)}
    added = 0
    for one in made:
        if (one.target_kind, one.target) in known:
            continue
        known.add((one.target_kind, one.target))
        temporary_data_add(state_dir, project_dir, command=one.command, cwd=one.cwd,
                           effect='worktree_remove' if one.what_key == 'tmp_what_worktree'
                           else 'delete_tree', origin=origin, kind='created',
                           what={'key': one.what_key, 'name': one.name},
                           target={'kind': one.target_kind, 'value': one.target})
        added += 1
    return added


def _is_item(value: object) -> bool:
    return (isinstance(value, dict) and isinstance(value.get('id'), str)
            and bool(_ITEM_ID_RE.fullmatch(value['id']))
            and isinstance(value.get('command'), str) and isinstance(value.get('cwd'), str)
            and isinstance(value.get('ts'), (int, float)) and not isinstance(value.get('ts'), bool))


def temporary_data_items(state_dir: str, project_dir: str) -> list[dict]:
    """One project's Temporary data, oldest first, at most TEMPORARY_DATA_MAX;
    a file that is not an item is left out."""
    folder = temporary_data_dir(state_dir, project_dir)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    items = [load_state(os.path.join(folder, name)) for name in names
             if _ITEM_ID_RE.fullmatch(name.removesuffix('.json')) and name.endswith('.json')]
    kept = [item for item in items if _is_item(item)]
    kept.sort(key=lambda item: item['ts'])
    return kept[:TEMPORARY_DATA_MAX]


def temporary_data_drop(state_dir: str, project_dir: str, item_id: str) -> None:
    if not _ITEM_ID_RE.fullmatch(item_id):
        raise ValueError(f'not an item id: {item_id!r}')
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(temporary_data_dir(state_dir, project_dir), f'{item_id}.json'))


GIT_PUSH_RE = re.compile(_GIT_PREFIX + r'push\b', re.IGNORECASE)

# Commands that create or rewrite commits, and so can leave a message with AI
# attribution in local history however that message was assembled.
_HISTORY_WRITE_SUBS = frozenset({'commit', 'commit-tree', 'merge', 'pull', 'cherry-pick',
                                 'rebase', 'revert', 'am'})

# Skill invocations that count as a review for the pre-push gate: the playbook's
# own review skill, and Claude Code's built-in /code-review.
REVIEW_SKILLS = ('engineering-code-review', 'code-review')

_SECRET_RE = re.compile(
    r'(?i)\b(token|secret|passwd|password|api[_-]?key|authorization|bearer)\b'
    # The value may be scheme-prefixed ("authorization: Bearer <token>"); the
    # scheme word must be consumed too, or the credential survives redaction.
    r'(?:\s*[=:]\s*|\s+)(?:(?:bearer|basic|token)\s+)?\S+'
)

JOURNAL_MAX_LINES = 200


def scrub_secrets(text: str) -> str:
    """Redact credential-shaped fragments before anything reaches the journal.

    The journal persists on disk across sessions and is read back into context
    by Learn (/playbook-config), so a token captured from a failed curl or an env dump
    would outlive the session that leaked it.

    Two passes: the keyword form (`password=...`) and the token shapes of the
    secret scanner's deny tier, which catch a bare token with no keyword next
    to it (a GitHub token echoed in a failing command's output)."""
    return _SECRET_RE.sub(lambda m: f'{m.group(1)}=***', secret_scan.redact(text or ''))


def journal_path(state_dir: str, session_id: str = '', cwd: str | None = None,
                 agent_id: str = '') -> str:
    """Failure journal for a session, alongside its state file."""
    path = state_path(state_dir, session_id, cwd, agent_id)
    return path.replace('.state-', '.learn-').replace('.json', '.jsonl')


def append_incident(path: str, record: dict, max_lines: int = JOURNAL_MAX_LINES) -> None:
    """Append one record, trimming the journal to its last `max_lines`. Never
    raises — a journal write must not be able to fail a tool call."""
    try:
        line = json.dumps(record, separators=(',', ':'), default=str)
        with open(path, 'a') as f:
            f.write(line + '\n')
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > max_lines * 2:
            with open(path, 'w') as f:
                f.writelines(lines[-max_lines:])
    except Exception:
        pass


def read_incidents(path: str, limit: int = 50) -> list[dict]:
    """Most recent incidents, oldest first. Never raises."""
    try:
        with open(path) as f:
            lines = f.readlines()[-limit:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ─── Deviations from a playbook rule ─────────────────────────────────────────
#
# A model that does not follow a skill rule, or follows one it believes wrong,
# reports it in a block of its response text; the Stop hook records it. The
# journal is one file for every project and kept across sessions: the rule
# belongs to the playbook, and it is the playbook that gets fixed.

DEVIATION_OPEN = '[PLAYBOOK-DEVIATION]'
DEVIATION_CLOSE = '[/PLAYBOOK-DEVIATION]'
DEVIATION_FIELDS = ('skill', 'rule', 'action', 'instead', 'why')
DEVIATION_ACTIONS = ('deviated', 'followed')
DEVIATION_MAX_LINES = 500
DEVIATION_FIELD_MAX_CHARS = 2000
DEVIATION_JOURNAL_ENV = 'PLAYBOOK_DEVIATION_JOURNAL'


def marker_block_re(open_marker: str, close_marker: str) -> re.Pattern[str]:
    """The text between two markers that each sit alone on their line. A
    sentence that only names the markers ("write between `[HANDOFF]` and
    `[/HANDOFF]`") is not a block. Accepted cost: a response that shows the
    format with the markers on their own lines, fenced or not, is read as a
    block; for a deviation the user sees it in the notice."""
    return re.compile(
        r'^[ \t]*' + re.escape(open_marker) + r'[ \t]*\n(.*?)^[ \t]*'
        + re.escape(close_marker) + r'[ \t]*$',
        re.DOTALL | re.MULTILINE,
    )


DEVIATION_RE = marker_block_re(DEVIATION_OPEN, DEVIATION_CLOSE)
_DEVIATION_FIELD_RE = re.compile(
    r'^[ \t]*[-*]?[ \t]*(' + '|'.join(DEVIATION_FIELDS) + r')[ \t]*:[ \t]*(.*)$', re.IGNORECASE)
_SKILLS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'skills')


class Deviation(NamedTuple):
    fields: dict[str, str]
    incomplete: tuple[str, ...]
    block: str


def _deviation_skill(value: str) -> str:
    return value.strip().strip('`').rsplit(':', 1)[-1].strip()


def parse_deviations(text: str) -> list[Deviation]:
    """Every deviation block in `text`, in order. A line that does not start a
    field continues the previous one. `incomplete` names the fields that are
    missing or hold a value the format does not allow (`instead` is required
    only when the rule was not followed)."""
    found: list[Deviation] = []
    for body in DEVIATION_RE.findall(text or ''):
        fields: dict[str, str] = {}
        current = ''
        for line in body.splitlines():
            match = _DEVIATION_FIELD_RE.match(line)
            if match:
                current = match.group(1).lower()
                fields[current] = match.group(2).strip()
            elif current and line.strip():
                fields[current] = (fields[current] + ' ' + line.strip()).strip()
        if 'skill' in fields:
            fields['skill'] = _deviation_skill(fields['skill'])
        if 'action' in fields:
            fields['action'] = fields['action'].strip().strip('`').lower()
        fields = {k: scrub_secrets(v)[:DEVIATION_FIELD_MAX_CHARS] for k, v in fields.items()}
        required = ['skill', 'rule', 'action', 'why']
        if fields.get('action') != 'followed':
            required.append('instead')
        incomplete = [f for f in required if not fields.get(f)]
        if fields.get('action') and fields['action'] not in DEVIATION_ACTIONS:
            incomplete.append('action')
        found.append(Deviation(fields, tuple(incomplete), body.strip()))
    return found


def deviation_journal_path(state_dir: str) -> str:
    """The journal every project writes to; the environment variable moves it
    (tests, a shared location)."""
    return os.environ.get(DEVIATION_JOURNAL_ENV) or os.path.join(state_dir, '.deviations.jsonl')


def skill_version(name: str) -> str:
    """The `version:` of the installed skill, '' when the playbook has no skill
    by that name: the record says which text of the rule the model read."""
    if not re.fullmatch(r'[\w-]+', name or ''):
        return ''
    try:
        with open(os.path.join(_SKILLS_ROOT, name, 'SKILL.md'), encoding='utf-8') as f:
            head = f.read(2048)
    except OSError:
        return ''
    match = re.search(r'^version:\s*([^\s]+)', head, re.MULTILINE)
    return match.group(1) if match else 'unversioned'


# ─── User preferences ─────────────────────────────────────────────────────────
#
# Runtime state, deliberately not shipped with the plugin: the plugin carries the
# DEFAULTS below, each user's own choices live in their state directory. Nothing
# here may be hardcoded to one machine — a value that only exists on the author's
# box is a value no other user of the playbook gets.

PROSE_LEVELS = ('low', 'mid', 'high')

# The session context is English; without this the model reads it as a cue and
# switches to English, most visibly right after a /clear. One text for every
# harness: the Claude Code and Codex SessionStart hooks inject it, and the
# Antigravity AGENTS.template.md carries it verbatim (a structural test holds
# the two equal).
USER_LANGUAGE_POLICY = (
    'Reply to the user in the language they write in, and keep that language '
    'after the context is cleared (/clear) or compacted: the English in this '
    'context is addressed to you, not a cue to switch the conversation to '
    "English. The playbook's menus and prompts follow the user's language too."
)

# Prose budgets. These are the product, not decoration: the difference between
# them is what the model is allowed to spend output tokens ON, and every level
# forbids the same filler — preamble, restating the request, recapping work the
# user just watched happen. A higher level buys substance, never ceremony.
PROSE_POLICY = {
    'low': """Prose budget: LOW. Give the result and only the reasoning that \
changes a decision. No preamble, no restating the request, no recap of steps the \
user just watched, no summary of what you are about to do, no unprompted offers \
of next steps. One or two sentences per point. Code, diffs and command output \
speak for themselves — do not narrate them. If something needs a caveat, make it \
one clause, not a paragraph.""",

    'mid': """Prose budget: MID. State what you did and why that approach, \
including any trade-off that would change the user's decision. Explain a \
non-obvious mechanism in one short paragraph. Cut the ceremony: no preamble, no \
restating context already established, no recap of steps already visible, no \
filler transitions. Detail is for the parts that are not self-evident from the \
code.""",

    'high': """Prose budget: HIGH. Explain the reasoning in full: why this \
approach over the alternatives you considered, the trade-offs, the failure modes, \
and how to verify the result. Ground it — cite file:line, command output, spec or \
documentation rather than asserting. Ceremony is still forbidden: no preamble, no \
flattery, no restating the request. High means more substance and more evidence, \
not more words around the same content.""",
}

PREFS_DEFAULTS: dict = {
    'prose': 'mid',    # response verbosity budget
    'watch': False,    # known-failure warnings before risky actions
    # Characters the project CLAUDE.md may hold before an edit that grows it blocks
    # the turn end. 0 turns the gate off. The harness's own ceiling is far higher
    # (it warns at 150k), but that ceiling is about loading at all; this one is
    # about cost — the file is re-sent to every session AND every subagent.
    'claude_md_budget': 40_000,
    # Tokens of context past which the session is told, once per multiple, to wrap
    # up and hand off. 0 = off. Not a window limit: on a 1M window the percentage
    # thresholds only speak at 700k, long after the cost has been paid — every call
    # re-reads the whole context, so a session's cost grows with its size squared.
    # 550k: the handoff and /clear come before the 700k warning on a 1M window;
    # on a smaller window the 70% guard speaks first and this never fires.
    'context_budget': 550_000,
    # Language of the fixed text the settings menu shows (playbook_i18n). 'auto'
    # follows the system locale; replies follow the language the user writes in.
    'language': 'auto',
    # Deviation reports to the maintainer (hooks/deviation_report.py). Off until
    # the user turns them on: a user who installed before the setting existed
    # never saw the first-run offer, and sending is never assumed.
    'reports': False,
    # Declined deletions kept in the Temporary data panel (hooks/temporary_data.tsx)
    # instead of dropped, so a leftover the user did not remove right away is
    # not forgotten by them or the model.
    'temporary_data': True,
}
PREF_FLAGS = ('watch', 'reports', 'temporary_data')

MEMORY_FILE = 'CLAUDE.md'


def text_chars(path: str) -> int | None:
    """Length of a text file in characters (the unit the harness budgets in), or
    None when it cannot be read — callers treat None as "no verdict"."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return len(f.read())
    except Exception:
        return None


def prefs_path(state_dir: str) -> str:
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, '.playbook-prefs.json')


def load_prefs(state_dir: str) -> dict:
    """Preferences merged over the defaults, with every value validated.

    Fail-open by construction: a corrupt, hand-edited or partially written file
    degrades to defaults rather than breaking every hook in the session."""
    prefs = dict(PREFS_DEFAULTS)
    try:
        with open(prefs_path(state_dir)) as f:
            stored = json.load(f)
    except Exception:
        return prefs
    if not isinstance(stored, dict):
        return prefs
    if stored.get('prose') in PROSE_LEVELS:
        prefs['prose'] = stored['prose']
    for flag in PREF_FLAGS:
        if isinstance(stored.get(flag), bool):
            prefs[flag] = stored[flag]
    budget = stored.get('claude_md_budget')
    if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
        prefs['claude_md_budget'] = budget
    tokens = stored.get('context_budget')
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        prefs['context_budget'] = tokens
    if stored.get('language') in playbook_i18n.LANGUAGES:
        prefs['language'] = stored['language']
    return prefs


_TOKEN_COUNT_RE = re.compile(r'^(\d+(?:\.\d+)?)([km]?)$')


def parse_token_count(raw: str) -> int | None:
    """'250000', '250k' or '1.5m' as an integer; None when it is none of those."""
    match = _TOKEN_COUNT_RE.match((raw or '').strip().lower().replace('_', ''))
    if not match:
        return None
    return int(float(match.group(1)) * {'': 1, 'k': 1_000, 'm': 1_000_000}[match.group(2)])


def set_pref(state_dir: str, key: str, value: str) -> tuple[dict, str | None]:
    """Validate and persist one preference. Returns (prefs, error)."""
    prefs = load_prefs(state_dir)
    key = (key or '').strip().lower()
    raw = (value or '').strip().lower()

    if key == 'prose':
        if raw not in PROSE_LEVELS:
            return prefs, f'prose must be one of {", ".join(PROSE_LEVELS)} — got {value!r}'
        prefs['prose'] = raw
    elif key in PREF_FLAGS:
        if raw in ('on', 'true', '1', 'yes'):
            prefs[key] = True
        elif raw in ('off', 'false', '0', 'no'):
            prefs[key] = False
        else:
            return prefs, f'{key} must be on or off — got {value!r}'
    elif key == 'claude_md_budget':
        if raw in ('off', 'none'):
            prefs['claude_md_budget'] = 0
        elif raw.isdigit():
            prefs['claude_md_budget'] = int(raw)
        else:
            return prefs, f'claude_md_budget must be a character count or off — got {value!r}'
    elif key == 'context_budget':
        tokens = 0 if raw in ('off', 'none') else parse_token_count(raw)
        if tokens is None:
            return prefs, f'context_budget must be a token count (250000, 250k) or off — got {value!r}'
        prefs['context_budget'] = tokens
    elif key == 'language':
        language = playbook_i18n.normalize(raw)
        if language is None:
            return prefs, (f'language must be one of {", ".join(playbook_i18n.LANGUAGES)} '
                           f'— got {value!r}')
        prefs['language'] = language
    else:
        return prefs, f'unknown preference {key!r} — valid keys: {", ".join(PREFS_DEFAULTS)}'

    try:
        save_state(prefs_path(state_dir), prefs)
    except Exception as exc:
        return prefs, f'could not write preferences: {exc}'
    return prefs, None
