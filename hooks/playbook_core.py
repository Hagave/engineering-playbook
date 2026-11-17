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
import glob
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import warnings
from typing import NamedTuple

import secret_scan

SPEC_TOKEN = '[SPEC CONFIRMED]'

# Source extensions for every language the playbook governs. The core rules are
# language-agnostic, so the gates must be too — an extension missing here silently
# disables the skill gate, the spec gate and the validation gate for that language.
_CODE_EXT = (
    r'\.(?:tsx|ts|jsx|js|mjs|cjs|vue|svelte'
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


def redirect_code_targets(command: str) -> list[str] | None:
    """Source files `command` writes through a redirect or `tee`, as written;
    None when it does not tokenize (the text patterns then apply, failing
    closed).

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
    return [t for t in targets if _ends_with_code_ext(t)]


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

DESTRUCTIVE_PATTERNS = [
    r'\brm\s+(?:[^\n]*\s)?(?:-[a-zA-Z]*[rR]|--recursive\b)',
    r'\bgit\s+push\b[^\n]*(?:\s-f\b|\s--force\b|\s\+\S+)',
    r'\bgit\s+reset\s+--hard\b',
    # Forced branch deletion in every spelling: -D, a -df/-fd cluster, and the
    # long forms in either order. A plain -d refuses an unmerged branch.
    # Case-sensitive on the short flags: the patterns run with IGNORECASE, and
    # read that way `-d` (safe delete) was `-D`.
    r'\bgit\s+branch\b[^\n;&|]*?\s(?:(?-i:-[a-zA-Z]*D)\b'
    r'|(?-i:-[a-zA-Z]*(?:d[a-zA-Z]*f|f[a-zA-Z]*d)[a-zA-Z]*)\b'
    r'|(?:--delete|-d)\b[^\n;&|]*\s(?:--force|-f)\b'
    r'|(?:--force|-f)\b[^\n;&|]*\s(?:--delete|-d)\b)',
    r'\bgit\s+checkout\s+--\s',
    # `git restore` discards working-tree changes unless it only unstages.
    # Short flags case-sensitive: under IGNORECASE `-s` (--source, which
    # overwrites the working tree) read as `-S` (--staged, which only unstages).
    r'\bgit\s+restore\b(?=[^\n;&|]*\s(?:--worktree|(?-i:-W))\b)',
    r'\bgit\s+restore\b(?![^\n;&|]*\s(?:--staged|(?-i:-S))\b)',
    r'\bgit\s+clean\s+(?:[^\n]*\s)?(?:-[a-zA-Z]*[fdx]\b|--force\b)',
    # History and stash that no reflog brings back.
    r'\bgit\s+stash\s+(?:clear|drop)\b',
    r'\bgit\s+reflog\s+(?:expire|delete)\b',
    r'\bgit\s+update-ref\s+(?:[^\n;&|]*\s)?-d\b',
    r'\bgit\s+filter-(?:branch|repo)\b',
    # A forced worktree removal discards its uncommitted changes.
    r'\bgit\s+worktree\s+remove\b[^\n;&|]*\s(?:--force|-f)\b',
    r'\brsync\b[^\n;&|]*\s--delete',
    r'\bshred\s',
    r'\btruncate\s+[^\n;&|]*(?:-s\s*0+\b|--size(?:=|\s+)0+\b)',
    r'\bfind\s+[^\n]*-delete\b',
    r'\bxargs\b[^\n]*\brm\b',
    r'\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\s+(?:IF\s+EXISTS\s+)?[`"\[]?\w',
    r'\bTRUNCATE\s+(?:TABLE\s+|ONLY\s+)?[`"\[]?\w+[`"\]]?\s*(?:;|$|\s+(?:CASCADE|RESTART))',
    r'(?s)\bDELETE\s+FROM\b(?!.*\bWHERE\b)',
]

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

# The user authorizing a destructive operation in their own words. Scoped to the
# message it appears in (see context_guard._destructive_authorized): the guard
# re-reads the latest user message on every call, so an authorization never
# outlives the turn that granted it, and the model can never grant itself one.
# Negation directly before a permission phrase inverts it: "não pode apagar" and
# "never delete the database" are prohibitions, and reading either as consent is
# the worst failure this pattern can have.
_NOT = r'(?<!não )(?<!nao )(?<!nunca )(?<!never )(?<!not )(?<!don\'t )'
# A bare imperative only counts where the sentence actually starts with it, or
# after an explicit yes. Otherwise any sentence that merely contains "delete the
# database" reads as an instruction to delete it.
_AFFIRMATIVE = r'(?:^|[.!?;]\s+|\b(?:yes|sim|ok|beleza|claro|please|por\s+favor),?\s+)'

# Every form names the destructive act itself. A general approval ("pode
# seguir", "pode rodar os testes", "I confirm the spec") answers another
# question, and read as consent it unlocked rm -rf and force push for the turn.
_PT_VERBS = 'apagar|deletar|remover|excluir|limpar|resetar|sobrescrever|forçar|forcar'
_PT_ACTS = r'exclus[ãa]o|remo[çc][ãa]o|reset|push\s+for[çc]ado'
_EN_VERBS = 'delete|remove|drop|wipe|reset|overwrite|force'
_EN_ACTS = r'deletion|removal|force\s+push'

DESTRUCTIVE_AUTHORIZED_RE = re.compile(
    _NOT + rf'\bpode\s+(?:{_PT_VERBS})\b'
    + '|' + _AFFIRMATIVE + r'(?:apaga|apague|deleta|remova|remove|exclua|limpa|limpe'
    r'|reseta|resete)\s+(?:isso|isto|esse|este|essa|esta|tudo|o\s+\S+|a\s+\S+)\b'
    + rf'|\b(?:eu\s+)?(?:autorizo|confirmo)\s+(?:(?:a|o)\s+)?(?:{_PT_VERBS}|{_PT_ACTS})\b'
    + rf'|\bgo\s+ahead\s+and\s+(?:{_EN_VERBS})\b'
    + '|' + _AFFIRMATIVE + rf'(?:{_EN_VERBS})\s+(?:it|this|that|them|the\s+\S+)\b'
    + rf'|\byou\s+(?:can|may)\s+(?:{_EN_VERBS})\b'
    + rf'|\bi\s+(?:authorize|confirm)\s+(?:the\s+)?(?:{_EN_VERBS}|{_EN_ACTS})\b',
    re.IGNORECASE | re.MULTILINE,
)

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


def _tokenize_segments(command: str) -> list[list[str]] | None:
    text = _unquoted_newlines_as_separators(command)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_SHELL_PUNCT)
    lexer.whitespace_split = True
    lexer.commenters = ''
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(ch in _SHELL_PUNCT for ch in token):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


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
    # to `$x -rf` must not read as `rm -rf` (that one is unattestable_command's).
    text = '\n'.join(' '.join(words) for words in map(command_words, segments) if words)
    return _GIT_GLOBAL_OPTS_RE.sub('git ', text)


def _starts_unknown(word: str) -> bool:
    return word.startswith(('$', '`'))


def _program_unknown(word: str) -> bool:
    """Whether the program NAME is decided at run time. `$JAVA_HOME/bin/java`
    is attestable — the directory varies, the program is `java` — while `$P`,
    `/usr/bin/$X` and `$(echo rm)` (read as `$SUBST`) are not."""
    return _starts_unknown(word.rsplit('/', 1)[-1])


def unattestable_command(command: str) -> str:
    """The first simple command whose program is decided at run time — a
    variable or a command substitution as the command word, or as the whole
    script of `eval` / `sh -c`. What such a command runs cannot be read before
    the shell runs it, so no pattern can clear it. '' when there is none."""
    for segment in shell_segments(command) or ():
        words = command_words(segment)
        if not words:
            continue
        base = words[0].rsplit('/', 1)[-1]
        if (_program_unknown(words[0])
                or (base == 'eval' and len(words) > 1 and _starts_unknown(words[1]))
                or (base in _SHELL_WORDS and '-c' in words[:-1]
                    and _starts_unknown(words[words.index('-c') + 1]))):
            return ' '.join(segment)
    return ''


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
    past the gate this closes."""
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
        if untracked_only and _tracked_by_git(full):
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


def _recursive_delete_call(source: ScriptSource) -> str:
    """The recursive-deletion call a python/other script makes, as text; ''
    when it makes none."""
    if source.language != 'python':
        match = _RECURSIVE_DELETE_API_RE.search(source.text)
        return match.group(0).strip() if match else ''
    tree = _python_tree(source.text)
    for node in ast.walk(tree) if tree is not None else ():
        if isinstance(node, ast.Call) and _call_name(node.func) in _PY_RECURSIVE_DELETE_CALLS:
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


def _simple_commands(command: str) -> tuple[list[list[str]], list[str]] | None:
    """The command words of every simple command in `command`, with the
    scripts it hands to a shell (see `_nested_scripts`) read as commands of
    their own; plus those nested scripts that do not tokenize, as text.

    A nested script that does not tokenize is returned for a text check and
    leaves the commands around it standing: dropping the whole command for it
    let `cp x.py src/ && bash -c "echo it's done"` hide the `cp`. None only
    when `command` itself does not tokenize."""
    segments = shell_segments(command)
    if segments is None:
        return None
    commands: list[list[str]] = []
    opaque: list[str] = []
    for segment in segments:
        words = command_words(segment)
        if not words:
            continue
        commands.append(words)
        for script in _nested_scripts(words):
            nested = _simple_commands(script)
            if nested is None:
                opaque.append(script)
            else:
                commands += nested[0]
                opaque += nested[1]
    return commands, opaque


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
    'The block lifts for the turn in which the user authorizes it in their own words '
    '(e.g. "pode apagar", "go ahead and delete it") — authorization does not carry to '
    'later turns, and you cannot grant it to yourself.'
)


def _destructive_block(reason: str) -> dict:
    return {'block': True, 'msg': f'[playbook:safety] {reason} {AUTHORIZATION_HINT}'}


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


def _pattern_lines(command: str, disposable_root: str = '') -> list[str]:
    """What the destructive patterns read: each simple command on a line of
    its own, quotes removed, search tools left out; once as written (`xargs
    rm` is a mass delete because of its wrapper) and once with wrappers and
    git's global options dropped (`sudo git -C . reset --hard`). Read as one
    line, `grep "find .*-exec"; grep "push --delete"` matched `find ...
    -delete` across two commands' arguments. The raw text when the command
    does not tokenize. An `rm` inside `disposable_root` is left out: see
    `removes_only_under`."""
    segments = shell_segments(command)
    if segments is None:
        return [command]
    lines = []
    for segment in segments:
        words = command_words(segment)
        if (words and not _searches_only(words)
                and not (segment == words and removes_only_under(words, disposable_root))):
            lines += [' '.join(segment), _GIT_GLOBAL_OPTS_RE.sub('git ', ' '.join(words))]
    return lines


def check_destructive(command: str, raw: str = '', cwd: str = '', _depth: int = 0,
                      disposable_root: str = '') -> dict | None:
    """Deny a command that destroys work, until the user authorizes it.

    `command` is the runnable command (heredoc bodies and inline scripts
    stripped). Four readings of it, each closing a way past the one before:

    1. the patterns, per simple command in its shell-normalized form, so
       quoting the command word or adding git global options does not hide it;
    2. a command whose program is decided at run time (`$x -rf`, `eval "$c"`),
       which no pattern can read;
    3. recursive deletion through an interpreter API in a `-c`/`-e` script,
       read in `raw` — the full command — because that source is stripped
       from `command` before step 1;
    4. the scripts the command runs (`script_sources`): a Python or other
       interpreter script for the same APIs, a shell script as a command. Of
       the script files, only those git does not track: see `script_sources`.

    Inside a script file (`_depth` > 0) step 2 is skipped: a variable command
    word there (`exec "$@"`) is the script's own logic, and the command the
    model typed was literal.

    `disposable_root` is the session's own scratchpad: an `rm` of literal paths
    inside it deletes only what the session made there, and needs no consent
    (decided by the user for 4.0.0; a build directory or node_modules still
    does)."""
    command = strip_heredoc_bodies(command)
    for line in _pattern_lines(command, disposable_root):
        for pattern in DESTRUCTIVE_PATTERNS:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                return _destructive_block(
                    f'Destructive operation detected (`{match.group(0).strip()}`). Confirm '
                    'with the user explicitly before proceeding: show the exact command and '
                    'what it destroys, and wait.')
    unknown = unattestable_command(command) if _depth == 0 else ''
    if unknown:
        return _destructive_block(
            f'Command decided at run time (`{unknown[:120]}`): its program comes from a '
            'variable or a command substitution, so the destructive check cannot read what '
            'it runs. Write the command literally; if it has to stay dynamic, show the user '
            'what it expands to and wait.')
    # Heredoc bodies that are data (a PR body, a doc) are left out: one quoting
    # `node -e "fs.rmSync(dir, opts)"` is text. A heredoc fed to a shell stays.
    for payload in inline_payloads(strip_heredoc_bodies(raw or command)):
        match = _RECURSIVE_DELETE_API_RE.search(payload)
        if match:
            return _destructive_block(
                f'Recursive deletion in an interpreter script (`{match.group(0).strip()}`). '
                'Confirm with the user explicitly before proceeding: show what it deletes, '
                'and wait.')
    if _depth == 0:
        for line in interpreter_shell_commands(raw or command, cwd):
            inner = check_destructive(strip_heredoc_bodies(line), line, cwd, _SCRIPT_MAX_DEPTH)
            if inner:
                return {**inner, 'msg': inner['msg'].replace(
                    '[playbook:safety] ', '[playbook:safety] In a command an interpreter script '
                    'runs: ', 1)}
    for source in script_sources(raw or command, cwd, untracked_only=True):
        if source.language == 'shell':
            if _depth >= _SCRIPT_MAX_DEPTH:
                continue
            text = _shell_script_text(source.text)
            inner = check_destructive(strip_inline_payloads(text), text, cwd, _depth + 1)
            if inner:
                return {**inner, 'msg': inner['msg'].replace(
                    '[playbook:safety] ', f'[playbook:safety] In the script `{source.origin}`: ', 1)}
            continue
        call = _recursive_delete_call(source)
        if call:
            return _destructive_block(
                f'Recursive deletion in the script `{source.origin}` (`{call}`). Confirm with '
                'the user explicitly before proceeding: show what it deletes, and wait.')
    return None


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


def _git_out(cwd: str, *args: str, timeout: float) -> str | None:
    """stdout of a git command, or None when it failed, timed out, or the
    hook has no time left for it (the caller decides what None means)."""
    timeout = _time_left(timeout)
    if timeout <= 0:
        return None
    try:
        done = subprocess.run(('git', '-C', cwd) + args,
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


# Options of `git push` that consume the next argument; every other option is
# a flag or takes its value after `=`.
_PUSH_VALUE_OPTS = frozenset({'-o', '--push-option', '--receive-pack', '--exec', '--repo'})
# A shell redirection among the words (`2>/dev/null`, `> log`, `2>`). Not a
# refspec: read as one, `git log` fails on it and the push fails closed.
_REDIRECT_RE = re.compile(r'^\d*(?:>>?|<)')
_REDIRECT_ONLY_RE = re.compile(r'^\d*(?:>>?|<)$')


def push_revisions(args: list[str]) -> list[str] | None:
    """Revisions a `git push` with these arguments (the words after `push`)
    would publish, as `git log` revision arguments.

    No refspec means the current branch, which is what `push.default` simple,
    current and upstream all push. `src:dst` publishes `src`, `:dst` and
    `--delete` publish nothing, a bare `:` publishes every matching branch
    (`--branches`), a glob goes through `--glob`, and so does a
    dry run. None when an argument is built at run time (a variable or a
    command substitution) and so cannot be known before the shell runs it."""
    revs: list[str] = []
    positional: list[str] = []
    deleting = skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT_RE.match(arg):
            skip_next = bool(_REDIRECT_ONLY_RE.match(arg))  # `> file`: the file too
            continue
        if '$' in arg or '`' in arg:
            return None
        if arg in ('-n', '--dry-run'):
            return []
        if arg in _PUSH_VALUE_OPTS:
            skip_next = True
        elif arg in ('--all', '--branches'):
            revs.append('--branches')
        elif arg == '--mirror':
            revs += ['--branches', '--tags']
        elif arg == '--tags':
            revs.append('--tags')
        elif arg in ('-d', '--delete'):
            deleting = True
        elif not arg.startswith('-'):
            positional.append(arg)
    refspecs = positional[1:]  # positional[0] is the remote
    if deleting:
        return revs
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
        if src:
            revs.append(f'--glob={src}' if '*' in src else src)
    if revs or refspecs:  # refspecs that were all `:dst` deletions publish nothing
        return revs
    return ['HEAD']


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
        revs = push_revisions(call.args)
        targets.append(PushTarget(call.repo, revs, '' if revs is not None
                                  else 'a refspec is built at run time'))
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

# Explicit, user-authored opt-out of the pre-push review gate. Only the user's
# own prompt is scanned for these — the model must never be able to clear its
# own gate by writing the phrase in its response.
# Every form names the review it waives: "pode pushar", "deixa assim" and "no
# review comments so far" are about the push or the code, and read as a waiver
# they skipped a review nobody declined.
REVIEW_BYPASS_RE = re.compile(
    r'\bsem\s+(?:review|revis[ãa]o|revisar)\b'
    r'|\bpul(?:a|ar|e)\s+(?:o\s+)?(?:review|revis[ãa]o)\b'
    r'|\b(?:skip|bypass|without)\s+(?:the\s+)?(?:code\s+)?review\b'
    r'|\bno\s+(?:code\s+)?review\s+(?:needed|required)\b',
    re.IGNORECASE,
)

GIT_PUSH_RE = re.compile(_GIT_PREFIX + r'push\b', re.IGNORECASE)

# Commands that create or rewrite commits, and so can leave a message with AI
# attribution in local history however that message was assembled.
_HISTORY_WRITE_SUBS = frozenset({'commit', 'commit-tree', 'merge', 'pull', 'cherry-pick',
                                 'rebase', 'revert', 'am'})

# A push that only inspects or tests the remote changes nothing — gating it
# would block the very command used to check what a push would do.
PUSH_NOOP_RE = re.compile(r'--dry-run\b|--porcelain\b', re.IGNORECASE)

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


def append_incident(path: str, record: dict) -> None:
    """Append one failure record, trimming the journal to its last
    JOURNAL_MAX_LINES. Never raises — a journal write must not be able to fail
    a tool call."""
    try:
        line = json.dumps(record, separators=(',', ':'), default=str)
        with open(path, 'a') as f:
            f.write(line + '\n')
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > JOURNAL_MAX_LINES * 2:
            with open(path, 'w') as f:
                f.writelines(lines[-JOURNAL_MAX_LINES:])
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


# ─── User preferences ─────────────────────────────────────────────────────────
#
# Runtime state, deliberately not shipped with the plugin: the plugin carries the
# DEFAULTS below, each user's own choices live in their state directory. Nothing
# here may be hardcoded to one machine — a value that only exists on the author's
# box is a value no other user of the playbook gets.

PROSE_LEVELS = ('low', 'mid', 'high')

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
    'context_budget': 0,
}

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
    if isinstance(stored.get('watch'), bool):
        prefs['watch'] = stored['watch']
    budget = stored.get('claude_md_budget')
    if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
        prefs['claude_md_budget'] = budget
    tokens = stored.get('context_budget')
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        prefs['context_budget'] = tokens
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
    elif key == 'watch':
        if raw in ('on', 'true', '1', 'yes'):
            prefs['watch'] = True
        elif raw in ('off', 'false', '0', 'no'):
            prefs['watch'] = False
        else:
            return prefs, f'watch must be on or off — got {value!r}'
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
    else:
        return prefs, f'unknown preference {key!r} — valid keys: {", ".join(PREFS_DEFAULTS)}'

    try:
        save_state(prefs_path(state_dir), prefs)
    except Exception as exc:
        return prefs, f'could not write preferences: {exc}'
    return prefs, None
