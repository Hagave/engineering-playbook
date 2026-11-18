#!/usr/bin/env python3
"""
Engineering Playbook — Context Guard
PreToolUse + Stop + SubagentStop hook: enforces context limits, blocks
destructive operations, strips Claude attribution from commits, enforces the
governance skill-load gate (pre-write), and — at turn end — the
spec-confirmation gate and the validation gate.

Registered via hooks/hooks.json. Runs before every tool call and on Stop.

The spec gate ([SPEC CONFIRMED]) is a Stop-time check, not pre-write: the token
is assistant TEXT emitted in the same turn as the guarded write, and the turn's
message is not flushed to the transcript until its tool calls resolve, so a
pre-write read cannot see it and would deny even when the model just confirmed.
Deferring to Stop (text flushed) removes that false positive while still
blocking a turn that wrote code without confirming.

Blocking contract (Claude Code hook semantics):
- PreToolUse deny  → exit 0 + JSON {"hookSpecificOutput": {"permissionDecision": "deny", ...}}
- Stop block       → exit 0 + JSON {"decision": "block", "reason": ...}
- Warning          → exit 0 + JSON {"systemMessage": ...}
Non-zero exit codes other than 2 are NOT blocking in Claude Code — never use
exit(1) to deny a tool call.

State is session-scoped (keyed by session_id from hook stdin) — multiple
simultaneous sessions in the same project never conflict. A subagent's hook
events (Agent, Workflow) carry its parent's session_id AND transcript_path;
only agent_id tells them apart. Subagents therefore get their own state
(session_id + agent_id) and their turn-end gates run at SubagentStop against
agent_transcript_path — otherwise a subagent's write arms the parent's Stop
gates, checked against the parent's transcript. The one crossing is read-only:
the parent's Stop counts a validation a subagent STARTED after the parent's
last write (the runner agent exists to run it cheaply), read from the
subagent's state file; nothing ever writes another agent's state.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import glob
import time

# Ensure hooks/ is importable regardless of how this file is loaded — as a
# script (python3 hooks/context_guard.py, where sys.path already includes
# it) or via importlib.util.spec_from_file_location (tests/unit/test_context_guard.py,
# which does not add the file's directory to sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core

# ─── Configuration ────────────────────────────────────────────────────────────

# The context window is resolved per-turn from the model actually in use
# (see _context_limit) — the guard must track whichever model the session is on
# instead of one global number.
#
# As of the Opus 4.6 / Sonnet 4.6 generation, 1M tokens is the STANDARD window
# for Opus 4.6+, Sonnet 4.6+ (incl. Sonnet 5), Fable 5, and Mythos — not a beta
# tier gated behind a "[1m]" alias. Only Haiku and legacy models (Opus <=4.5,
# Sonnet <=4.5 and older) remain at 200k by default; those legacy models can
# still opt into the 1M beta via an explicit "[1m]" alias.
DEFAULT_CONTEXT_LIMIT = 200_000      # legacy/Haiku window, and the unknown-model fallback
ONE_M_LIMIT           = 1_000_000    # standard window for 4.6+/5-gen models; beta for legacy models

# Self-calibration ladder: if observed usage ever exceeds the resolved limit
# (unknown model, undetected beta, resumed session), snap up to the next tier so
# the guard can never hard-block on tokens the window has already proven to hold.
CONTEXT_TIERS = (ONE_M_LIMIT, 2_000_000, 5_000_000)

# Model families whose STANDARD window is already 1M — matched against the
# lowercased model id from the transcript (e.g. "claude-sonnet-4-6",
# "claude-opus-4-8", "claude-fable-5"). Legacy Opus (<=4.5) and Sonnet (<=4.5)
# are deliberately excluded — those stay at 200k unless "[1m]"-tagged.
# Open-ended major ranges (opus-[5-9] and friends) rather than an exact list:
# claude-opus-5 shipped with a 1M standard window and was silently resolved as
# a 200k legacy model here, hard-blocking sessions at a fifth of their real
# window. Pinning exact versions means re-breaking this on every release.
_STANDARD_1M_RE = re.compile(
    r'opus-4-[6-9]|opus-[5-9]|sonnet-4-[6-9]|sonnet-[5-9]|fable-[5-9]|mythos',
    re.IGNORECASE,
)

# 1M-beta-capable families (legacy models only) — Haiku never qualifies, even
# under a "[1m]" alias.
_ONE_M_FAMILIES = ('opus', 'sonnet', 'fable')

# PLAYBOOK_CONTEXT_LIMIT still forces a fixed window when set — an explicit
# escape hatch that overrides model resolution. Left unset, resolution is adaptive.
CONTEXT_LIMIT  = int(os.environ.get('PLAYBOOK_CONTEXT_LIMIT') or DEFAULT_CONTEXT_LIMIT)
WARN_PCT       = 0.70      # warn — avoid new skills, finish current task
BLOCK_PCT      = 0.85      # hard block — no tool calls; handoff + /clear
UNLOCK_PCT     = 0.60      # lock releases after context drops here
TAIL_BYTES     = 262_144   # transcript tail window for per-call scans

# Destructive-command patterns, commit-attribution regex, source-file
# extension matching, the bash-code-write heuristic, and the spec token are
# shared with the Codex adapter guards — see hooks/playbook_core.py.
DESTRUCTIVE_PATTERNS = playbook_core.DESTRUCTIVE_PATTERNS
SPEC_TOKEN = playbook_core.SPEC_TOKEN
CODE_EXT_RE = playbook_core.CODE_EXT_RE
BASH_CODE_WRITE_RE = playbook_core.BASH_CODE_WRITE_RE
VALIDATION_RE = playbook_core.VALIDATION_RE
SUSPEND_SKILL = playbook_core.SUSPEND_SKILL
RESUME_SKILLS = playbook_core.RESUME_SKILLS
LEARNED_TOKEN = playbook_core.LEARNED_TOKEN
DESTRUCTIVE_AUTHORIZED_RE = playbook_core.DESTRUCTIVE_AUTHORIZED_RE
REVIEW_SKILLS = playbook_core.REVIEW_SKILLS
PUSH_NOOP_RE = playbook_core.PUSH_NOOP_RE
REVIEW_BYPASS_RE = playbook_core.REVIEW_BYPASS_RE

IMPL_TOOLS = {'Write', 'Edit', 'MultiEdit'}

# Known-failure lookup budget. The warning has to be cheaper than the mistake it
# prevents, so it is capped hard and deduplicated per session: the same rule
# repeated every turn stops being read after the second time.
WATCH_MAX_HITS = 3
WATCH_MAX_CHARS = 220
LIVE_RULES_RE = re.compile(
    r'<!--\s*engineering-learn:live\s*-->(.*?)<!--\s*/engineering-learn:live\s*-->',
    re.DOTALL | re.IGNORECASE,
)

GOVERNANCE_SKILL = 'engineering-governance'
STATE_DIR = os.path.expanduser('~/.claude/hooks')

COMMAND_NAME_RE = re.compile(r'<command-name>/?([\w:.-]+)</command-name>')

# ─── Session-scoped state ─────────────────────────────────────────────────────

_SESSION_ID = ''  # set by main() from hook stdin
_AGENT_ID = ''    # set by main(); non-empty only for a subagent's own events
_CWD = ''         # set by main(); the repo the hook is guarding


def _state_path() -> str:
    return playbook_core.state_path(os.path.expanduser('~/.claude/hooks'), _SESSION_ID, agent_id=_AGENT_ID)


def _load_state():
    return playbook_core.load_state(_state_path())


def _save_state(state: dict) -> None:
    playbook_core.save_state(_state_path(), state)


# Validation commands a subagent has started and not yet seen finish, keyed by
# tool_use_id: the start time decides whether the run can have seen the
# parent's last write. Bounded, since a denied call never reaches PostToolUse.
_VALIDATION_STARTS_KEPT = 8


def _is_validation(tool_name: str, command: str) -> bool:
    """A Bash command that validates, read with heredoc bodies and interpreter
    payloads stripped: a doc that merely mentions `npm test` validates nothing."""
    return tool_name == 'Bash' and bool(VALIDATION_RE.search(
        playbook_core.strip_inline_payloads(playbook_core.strip_heredoc_bodies(command))))


def _note_validation_start(state: dict, tool_use_id: str) -> None:
    starts = state.get('validation_starts')
    starts = starts if isinstance(starts, dict) else {}
    starts[tool_use_id] = time.time()
    state['validation_starts'] = dict(list(starts.items())[-_VALIDATION_STARTS_KEPT:])
    _save_state(state)


def _note_validation_done(state: dict, tool_use_id: str) -> None:
    starts = state.get('validation_starts')
    started = starts.pop(tool_use_id, None) if isinstance(starts, dict) else None
    if isinstance(started, (int, float)):
        state['validated_from'] = max(float(state.get('validated_from') or 0), float(started))


def _validated_by_subagent(state: dict) -> bool:
    """Whether a subagent of this session ran a validation that started after
    the parent's last code write. A run already going when the write landed
    may not have seen it, so it does not count; neither does a pending flag
    from a state file older than the timestamp."""
    written = state.get('pending_validation_at')
    if not isinstance(written, (int, float)):
        return False
    for path in playbook_core.subagent_state_paths(STATE_DIR, _SESSION_ID):
        validated = playbook_core.load_state(path).get('validated_from')
        if isinstance(validated, (int, float)) and validated >= written:
            return True
    return False

# ─── Transcript access ────────────────────────────────────────────────────────

def _session_dir() -> str:
    slug = os.getcwd().lstrip('/').replace('/', '-')
    return os.path.expanduser(f'~/.claude/projects/{slug}')


def _latest_jsonl() -> str | None:
    """Fallback only — used when the hook input carries no transcript_path."""
    files = glob.glob(os.path.join(_session_dir(), '*.jsonl'))
    return max(files, key=os.path.getmtime) if files else None


def _read_tail_lines(path: str) -> list[str]:
    """Read the last TAIL_BYTES of the transcript — full reads on every tool
    call do not scale once sessions grow to tens of MB."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # discard partial line
            data = f.read()
        return data.decode('utf-8', errors='replace').splitlines()
    except Exception:
        return []

# ─── Transcript entry helpers ─────────────────────────────────────────────────

def _assistant_text(obj: dict) -> str:
    if obj.get('type') != 'assistant':
        return ''
    content = obj.get('message', {}).get('content', '')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(
            b.get('text', '') for b in content
            if isinstance(b, dict) and b.get('type') == 'text'
        )
    return ''


_NON_PROMPT_RE = playbook_core.NON_PROMPT_RE


def _is_prompt_text(text: str) -> bool:
    return bool(text.strip()) and not _NON_PROMPT_RE.match(text)


def _is_user_prompt(obj: dict) -> bool:
    """A real user message — not a tool_result, not a meta entry, and not
    harness-injected system content (system-reminders, task notifications)."""
    if obj.get('type') != 'user' or obj.get('isMeta'):
        return False
    content = obj.get('message', {}).get('content')
    if isinstance(content, str):
        return _is_prompt_text(content)
    if isinstance(content, list):
        # Tool results may carry injected text blocks — never a user prompt.
        if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            return False
        return any(
            isinstance(b, dict) and b.get('type') == 'text'
            and _is_prompt_text(b.get('text', ''))
            for b in content
        )
    return False


def _skill_uses(obj: dict) -> list[str]:
    """Skill names invoked in this entry — Skill tool calls and slash commands."""
    names: list[str] = []
    content = obj.get('message', {}).get('content')
    if obj.get('type') == 'assistant' and isinstance(content, list):
        for b in content:
            if (isinstance(b, dict) and b.get('type') == 'tool_use'
                    and b.get('name') == 'Skill'):
                inp = b.get('input', {})
                value = inp.get('skill') or inp.get('command') or ''
                if value:
                    names.append(str(value))
    elif obj.get('type') == 'user':
        text = content if isinstance(content, str) else ''
        if isinstance(content, list):
            text = ' '.join(
                b.get('text', '') for b in content
                if isinstance(b, dict) and b.get('type') == 'text'
            )
        names.extend(COMMAND_NAME_RE.findall(text))
    return names

# ─── Context measurement ──────────────────────────────────────────────────────

def _settings_model_alias() -> str:
    """The model the session is configured to use, read from the Claude Code
    settings cascade (project local → project → user). This is the only place
    the 1M-beta "[1m]" marker survives: the transcript records the base model id
    (e.g. "claude-opus-4-8") with the beta suffix stripped."""
    candidates = (
        os.path.join(os.getcwd(), '.claude', 'settings.local.json'),
        os.path.join(os.getcwd(), '.claude', 'settings.json'),
        os.path.expanduser('~/.claude/settings.local.json'),
        os.path.expanduser('~/.claude/settings.json'),
    )
    for path in candidates:
        try:
            with open(path) as f:
                model = json.load(f).get('model')
            if model:
                return str(model)
        except Exception:
            continue
    return ''


def _context_limit(model: str) -> int:
    """Resolve the context window for the model in use, adaptively.

    Priority: explicit PLAYBOOK_CONTEXT_LIMIT override → Haiku always stays at
    the legacy window → models whose STANDARD window is already 1M (Opus 4.6+,
    Sonnet 4.6+/5, Fable 5, Mythos) → legacy models running under the 1M beta
    ("[1m]" alias) → the legacy 200k default. Haiku never qualifies for 1M,
    even when the configured alias carries "[1m]"."""
    override = os.environ.get('PLAYBOOK_CONTEXT_LIMIT')
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    m = (model or '').lower()
    if 'haiku' in m:
        return DEFAULT_CONTEXT_LIMIT
    if _STANDARD_1M_RE.search(m):
        return ONE_M_LIMIT
    one_m_capable = '[1m]' in m or any(f in m for f in _ONE_M_FAMILIES)
    if one_m_capable and ('[1m]' in m or '[1m]' in _settings_model_alias().lower()):
        return ONE_M_LIMIT
    return DEFAULT_CONTEXT_LIMIT


def _calibrate(limit: int, total: int) -> int:
    """Raise the limit to the next known tier above observed usage. Guards
    against underestimating an unknown model's window: usage the window has
    already held is proof the real window is at least that large."""
    if total <= limit:
        return limit
    for tier in CONTEXT_TIERS:
        if tier > total:
            return tier
    return total


def _measure_context(transcript: str | None) -> tuple[float, int] | None:
    """Return (fraction_used, resolved_limit) for the latest assistant turn, or
    None when no usage is available. The limit tracks the turn's own model."""
    if not transcript or not os.path.exists(transcript):
        return None
    for line in reversed(_read_tail_lines(transcript)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get('type') == 'assistant' and 'message' in obj:
            message = obj['message']
            usage = message.get('usage', {})
            if usage:
                # output_tokens counts: the next request replays this
                # turn's output as input, so a window check that omits it
                # reports a figure the session has already exceeded — up to a
                # full max_tokens of drift right when the margin matters.
                total = (
                    usage.get('input_tokens', 0)
                    + usage.get('cache_creation_input_tokens', 0)
                    + usage.get('cache_read_input_tokens', 0)
                    + usage.get('output_tokens', 0)
                )
                limit = _calibrate(_context_limit(message.get('model', '')), total)
                return total / limit, limit
    return None

# ─── Git helpers ──────────────────────────────────────────────────────────────

def _git(*args: str) -> str:
    """Git output, or '' on any failure. Fail-open by construction: a gate that
    cannot read the repo must not block the tool call."""
    try:
        out = subprocess.run(
            ('git', '-C', _CWD or os.getcwd()) + args,
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else ''
    except Exception:
        return ''


def _git_head() -> str:
    return _git('rev-parse', 'HEAD')


def _push_range() -> str:
    """The diff range a push would publish: everything HEAD has that its
    upstream does not, falling back to the default branch for a branch that has
    no upstream yet."""
    upstream = _git('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    if upstream:
        return f'{upstream}...HEAD'
    head = _git('symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
    return f'{head or "origin/main"}...HEAD'


def _nothing_to_push() -> bool:
    """True only when the upstream already contains HEAD. A branch with no
    upstream returns '' and is treated as having something to push."""
    return _git('rev-list', '--count', '@{u}..HEAD') == '0'


# ─── Checks ───────────────────────────────────────────────────────────────────

# The model's own account of the task, captured from its response TEXT at Stop.
# Text, not a file write: at BLOCK_PCT every tool call is denied, and the handoff
# is exactly what still has to get out at that point.
HANDOFF_OPEN = '[HANDOFF]'
HANDOFF_CLOSE = '[/HANDOFF]'
# Markers must sit on lines of their own: a response that merely MENTIONS them
# inline ("write between `[HANDOFF]` and `[/HANDOFF]`") would otherwise save the
# text between the two mentions as a handoff, overwriting a real one.
HANDOFF_RE = re.compile(
    r'^[ \t]*' + re.escape(HANDOFF_OPEN) + r'[ \t]*\n(.*?)^[ \t]*' + re.escape(HANDOFF_CLOSE) + r'[ \t]*$',
    re.DOTALL | re.MULTILINE,
)

_HANDOFF_FORMAT = (
    f'Write the handoff in your response text between a {HANDOFF_OPEN} line and a '
    f'{HANDOFF_CLOSE} line, each marker alone on its line — '
    'no tool call needed, the Stop hook saves it to disk: the goal as the user stated it, '
    'what is done, what is pending with the exact next step, decisions and constraints '
    'already agreed, files involved, and any gate still owed. '
)

_CLEAR_INSTRUCTION = (
    'Then tell the user to run /clear and, in the new session, ask to resume where it left '
    'off — the handoff and the carry-over are re-injected automatically on that /clear. '
    'Do not recommend /compact: it does not reliably bring the session back under the '
    'threshold, and a compact that falls short leaves this guard blocking.'
)


def _check_context(pct: float | None, state: dict, limit: int = CONTEXT_LIMIT) -> dict | None:
    if pct is None:
        return None

    if pct >= BLOCK_PCT:
        state['locked'] = True
        state['lock_ts'] = time.time()
        _save_state(state)
        used = int(pct * limit)
        return {
            'block': True,
            'msg': (
                f'[playbook:context] {pct:.0%} used ({used:,}/{limit:,} tokens). '
                'HARD STOP — no further tool calls; do not try to finish the task. '
                + _HANDOFF_FORMAT
                + 'Include any code you generated this turn that is not yet on disk. '
                + _CLEAR_INSTRUCTION
            ),
        }

    if pct >= WARN_PCT:
        used = int(pct * limit)
        # The model is the one that must wrap up, and a systemMessage alone only
        # reaches the user. Sent to the model once per crossing: repeated on every
        # call it would itself fill the context it warns about.
        to_model = not state.get('context_warned')
        state['context_warned'] = True
        return {
            'block': False,
            'to_model': to_model,
            'msg': (
                f'[playbook:context] {pct:.0%} used ({used:,}/{limit:,} tokens). '
                'Do not load new skills or start new work. If a task is in progress, finish '
                'it; then end the turn. '
                + _HANDOFF_FORMAT
                + _CLEAR_INSTRUCTION
            ),
        }

    state.pop('context_warned', None)  # below the line again (compaction): re-arm
    return None


def _check_budget(total: int | None, state: dict, prefs: dict) -> dict | None:
    """Advise wrapping up once the context passes the user's token budget.

    A cost threshold, not a safety one, so it never blocks and it speaks once per
    multiple of the budget (1x, 2x, ...): a notice on every call would itself be
    context the next call re-reads. It goes to the MODEL as additionalContext —
    a systemMessage alone is shown to the user and changes nothing. Subagents are
    skipped: they cannot be /clear-ed, and their context dies with them."""
    budget = prefs.get('context_budget', 0)
    if not budget or total is None or _AGENT_ID:
        return None
    level = total // budget
    if level <= int(state.get('budget_level', 0)):
        if level < int(state.get('budget_level', 0)):
            state['budget_level'] = level   # usage dropped (compaction): re-arm
        return None
    state['budget_level'] = level
    return {
        'block': False,
        'to_model': True,
        'msg': (
            f'[playbook:context] {total:,} tokens of context, past the user\'s budget of '
            f'{budget:,} ({level}x). This is a cost budget, not the window limit: tools still '
            'run, but every call from here re-reads all of it. Do not start new work. Finish '
            'the step in progress; at the next natural stopping point end the turn. '
            + _HANDOFF_FORMAT
            + _CLEAR_INSTRUCTION
        ),
    }


def _check_lock(state: dict, pct: float | None) -> dict | None:
    if not state.get('locked'):
        return None

    # Auto-unlock when usage drops (e.g. the harness's own auto-compaction)
    if pct is not None and pct < UNLOCK_PCT:
        state['locked'] = False
        state.pop('lock_ts', None)
        _save_state(state)
        return None

    # Safety: auto-unlock after 2h even if pct is unavailable (session likely ended)
    lock_ts = state.get('lock_ts')
    if lock_ts and (time.time() - lock_ts) > 7200:
        state['locked'] = False
        state.pop('lock_ts', None)
        _save_state(state)
        return None

    return {
        'block': True,
        'msg': (
            '[playbook:context] Session is locked — context was above '
            f'{BLOCK_PCT:.0%}. No tool call will run. ' + _HANDOFF_FORMAT + _CLEAR_INSTRUCTION
        ),
    }


_check_destructive = playbook_core.check_destructive


def _session_scratchpad() -> str:
    """This session's scratchpad, `<tmp>/claude-<uid>/<project>/<session>/scratchpad`,
    or '' when it cannot be found exactly once. Only this session's: another
    session's scratchpad is not what this one made."""
    if not _SESSION_ID or not hasattr(os, 'getuid'):
        return ''
    base = os.path.join(tempfile.gettempdir(), f'claude-{os.getuid()}')
    found = glob.glob(os.path.join(glob.escape(base), '*', glob.escape(_SESSION_ID), 'scratchpad'))
    return found[0] if len(found) == 1 else ''


_check_ai_attribution = playbook_core.check_ai_attribution


def _scan_skills(transcript: str | None, state: dict) -> bool:
    """Incrementally scan the transcript for Skill invocations.

    Updates state['gov_loaded'] and state['suspended']; caches the scan offset
    so each call only reads bytes appended since the previous call.
    Returns the current suspended flag.
    """
    if not transcript or not os.path.exists(transcript):
        return bool(state.get('suspended'))
    offset = state.get('scan_offset', 0)
    try:
        size = os.path.getsize(transcript)
        if size < offset:  # rotated or truncated — rescan
            offset = 0
            state['gov_loaded'] = False
            state['suspended'] = False
        with open(transcript, encoding='utf-8', errors='replace') as f:
            f.seek(offset)
            for line in f:
                if 'engineering-' not in line and 'playbook-' not in line and 'code-review' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                for name in _skill_uses(obj):
                    base = name.rsplit(':', 1)[-1]
                    if base == SUSPEND_SKILL:
                        state['suspended'] = True
                    elif base in RESUME_SKILLS:
                        state['suspended'] = False
                    elif base == GOVERNANCE_SKILL:
                        state['gov_loaded'] = True
                    if base in REVIEW_SKILLS:
                        # Pins the review to the commit it actually read. A
                        # commit made after the review is unreviewed code, and
                        # the push gate must see that.
                        head = _git_head()
                        if head:
                            state['reviewed_head'] = head
                        # Findings are only in the text this turn flushes, so the
                        # check itself has to wait for Stop.
                        state['review_turn'] = True
                    if base.startswith('engineering-'):
                        loaded = state.get('skills')
                        if not isinstance(loaded, list):
                            loaded = []
                        if base not in loaded:
                            loaded.append(base)
                        state['skills'] = loaded[-24:]
            state['scan_offset'] = f.tell()
    except Exception:
        pass
    return bool(state.get('suspended'))


def _check_skill_gate(state: dict) -> dict | None:
    if state.get('gov_loaded'):
        return None
    return {
        'block': True,
        'msg': (
            '[playbook:skill-gate] engineering-governance has not been loaded this session. '
            'Invoke the Skill tool with "engineering-governance" (which mandates its companion '
            'skills) before writing any code. See the routing table in engineering-playbook.'
        ),
    }


def _token_after_last_prompt(transcript: str | None, token: str) -> bool:
    """Whether the model emitted `token` in assistant text after the most recent
    real user message — i.e. in the current turn, not in an earlier one.

    Three outcomes, not two: the token came first (confirmed), a user message
    came first (genuinely unconfirmed), or neither is in the window at all.

    The third case only exists because the scan is bounded to TAIL_BYTES: a turn
    with large tool output pushes both the token and the prompt out of the
    window, and treating that as "no confirmation" blocks a turn that did
    confirm — measured on a turn whose tool output alone exceeded the window.

    Absence of evidence is only fail-open when the window actually truncated the
    history. When the whole transcript fits inside it, a missing token really is
    a missing token, and the gate must still fire."""
    if not transcript or not os.path.exists(transcript):
        return True
    try:
        truncated = os.path.getsize(transcript) > TAIL_BYTES
    except OSError:
        truncated = False
    for line in reversed(_read_tail_lines(transcript)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if token in _assistant_text(obj):
            return True
        if _is_user_prompt(obj):
            return False  # the turn is in the window and the token is not in it
    return truncated


def _latest_user_prompt(transcript: str | None) -> str:
    """Text of the most recent real user message, or ''."""
    if not transcript or not os.path.exists(transcript):
        return ''
    for line in reversed(_read_tail_lines(transcript)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not _is_user_prompt(obj):
            continue
        content = obj.get('message', {}).get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return ' '.join(
                b.get('text', '') for b in content
                if isinstance(b, dict) and b.get('type') == 'text'
            )
    return ''


def _destructive_authorized(transcript: str | None) -> bool:
    """Whether the user authorized a destructive operation in their current
    message.

    Read fresh from the latest user message on every call, which is what scopes
    it: an authorization is good for the turn it was given in and no longer. It
    is deliberately not persisted — a stored flag would let a "pode apagar" from
    an hour ago clear a block on something the user never saw. And because only
    user messages are scanned, the model cannot authorize itself by writing the
    phrase in its own response."""
    prompt = _latest_user_prompt(transcript)
    return bool(prompt) and bool(DESTRUCTIVE_AUTHORIZED_RE.search(prompt))


_FINDING_RE = re.compile(r'^\s*[-*]\s+`?[^`\n]+?:\d+`?\s*[—-]', re.MULTILINE)
_SEVERITY_RE = re.compile(r'^#{1,4}\s*(blockers?|major)\b', re.IGNORECASE | re.MULTILINE)


def _review_findings(transcript: str | None) -> int:
    """How many Blocker/Major findings the review reported in this turn.

    Reads the review's own output format (a severity heading followed by
    `path:line — …` bullets). A review that found nothing writes "none" under the
    heading and produces no bullets, so a clean review never arms anything."""
    if not transcript or not os.path.exists(transcript):
        return 0
    text_blocks: list[str] = []
    for line in reversed(_read_tail_lines(transcript)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if _is_user_prompt(obj):
            break
        text = _assistant_text(obj)
        if text:
            text_blocks.append(text)
    findings = 0
    for text in text_blocks:
        for match in _SEVERITY_RE.finditer(text):
            section = text[match.end():]
            next_heading = re.search(r'^#{1,4}\s', section, re.MULTILINE)
            if next_heading:
                section = section[:next_heading.start()]
            findings += len(_FINDING_RE.findall(section))
    return findings


def _arm_review_learning(state: dict, transcript: str | None) -> None:
    """Feed the review's own findings back into the learn loop.

    A review that keeps reporting the same defect class is describing a habit,
    not an incident. Findings are journaled for Learn (/playbook-config) and the learn
    gate is armed so the turn cannot end until the rule that prevents them is
    written down."""
    if state.get('learn_pending') or not state.get('review_turn'):
        return
    findings = _review_findings(transcript)
    state['review_turn'] = False
    if not findings:
        return
    try:
        playbook_core.append_incident(
            playbook_core.journal_path(STATE_DIR, _SESSION_ID, agent_id=_AGENT_ID),
            {'ts': time.time(), 'kind': 'review_finding', 'cwd': _CWD,
             'count': findings, 'head': state.get('reviewed_head', '')[:12]},
        )
    except Exception:
        pass
    state['learn_pending'] = True
    state['learn_reason'] = (
        f'[playbook:learn] The review reported {findings} Blocker/Major finding(s) in code '
        'you wrote. Fixing them is not the same as not repeating them.\n'
        'Before ending this turn: name the defect class behind the findings (not each '
        'finding), state the rule that would have prevented it, persist that rule in the '
        f'project CLAUDE.md inside the `<!-- engineering-learn:live -->` block, and emit '
        f'{LEARNED_TOKEN}.\n'
        f'If the findings share no common cause, emit `{LEARNED_TOKEN} none — one-off findings`.'
    )


# A rule in the live block is meant to be one line. Past this it is carrying the
# incident with it, which is what a budget overrun is usually made of.
LIVE_RULE_MAX_CHARS = 400


def _memory_target(tool_name: str, tool_input: dict) -> str:
    """Path of the CLAUDE.md this call writes, '' when it writes none."""
    if tool_name not in IMPL_TOOLS:
        return ''
    path = str(tool_input.get('file_path') or '')
    return path if os.path.basename(path) == playbook_core.MEMORY_FILE else ''


def _note_memory_write(state: dict, tool_name: str, tool_input: dict) -> None:
    """Remember how large a CLAUDE.md was before this turn first touched it.

    The gate judges GROWTH, not size: a project that arrives over budget must
    stay editable, and an edit that shrinks an oversized file is the fix, not the
    offence. Without the 'before' figure the two cannot be told apart."""
    path = _memory_target(tool_name, tool_input)
    if not path:
        return
    sizes = state.setdefault('memory_before', {})
    if path not in sizes:
        sizes[path] = playbook_core.text_chars(path) or 0


def _check_memory_budget(state: dict, prefs: dict) -> dict | None:
    """Block the turn end when a CLAUDE.md was grown past the user's budget.

    Consumes the 'before' figures, so it fires once per turn that grew the file;
    the stop_hook_active pass that follows a block returns before reaching here
    and leaves a later write for the next turn's end to judge."""
    before = state.pop('memory_before', None)
    budget = prefs.get('claude_md_budget', 0)
    if not budget or not isinstance(before, dict):
        return None
    for path, was in before.items():
        now = playbook_core.text_chars(path)
        if now is None or now <= budget or now <= int(was or 0):
            continue
        long_rules = sum(1 for rule in _live_rules(os.path.dirname(path))
                         if len(rule) > LIVE_RULE_MAX_CHARS)
        return {
            'block': True,
            'msg': (
                f'[playbook:memory] {path} grew from {int(was or 0):,} to {now:,} characters, '
                f'past the {budget:,} budget. It is re-sent to every session and every '
                f'subagent, so its size is paid on each of them. Keep each rule there as ONE '
                f'line and move the incident behind it (dates, quotes, sub-cases) to a file '
                f'CLAUDE.md does not @-import, leaving a pointer to it.'
                + (f' {long_rules} rule(s) in the engineering-learn:live block exceed '
                   f'{LIVE_RULE_MAX_CHARS} characters — start there.' if long_rules else '')
                + ' The budget is the user\'s: `playbook_config.py set claude_md_budget '
                  '<chars|off>`; never change it yourself to clear this gate.'
            ),
        }
    return None


def _live_rules(cwd: str) -> list[str]:
    """Rules the learn gate has written into this project's CLAUDE.md."""
    try:
        with open(os.path.join(cwd, 'CLAUDE.md'), encoding='utf-8', errors='replace') as f:
            block = LIVE_RULES_RE.search(f.read())
    except Exception:
        return []
    if not block:
        return []
    return [line.strip(' -*\t') for line in block.group(1).splitlines()
            if line.strip(' -*\t') and not line.lstrip().startswith('#')]


def _watch_targets(tool_name: str, tool_input: dict) -> list[str]:
    """The identifiers a known failure would have been recorded against."""
    targets: list[str] = []
    path = str(tool_input.get('file_path') or '')
    if path:
        targets += [os.path.basename(path), path]
    command = str(tool_input.get('command') or '')
    if command:
        words = re.findall(r'[A-Za-z0-9_./-]{3,}', command)[:6]
        targets += words
    return [t for t in targets if len(t) >= 3]


def _check_watch(state: dict, prefs: dict, tool_name: str, tool_input: dict) -> dict | None:
    """Surface failures already recorded for this project, before repeating them.

    This is retrieval, not supervision: a background agent cannot interrupt the
    main one, so the warning has to arrive through the hook that already runs
    before every tool call. What it searches is evidence — incidents the learn
    journal captured and rules the learn gate wrote — never a model's opinion."""
    if not prefs.get('watch'):
        return None
    if tool_name not in IMPL_TOOLS and tool_name != 'Bash':
        return None

    targets = _watch_targets(tool_name, tool_input)
    if not targets:
        return None

    seen = state.get('watch_seen')
    if not isinstance(seen, list):
        seen = []

    candidates: list[str] = []
    for rule in _live_rules(_CWD or os.getcwd()):
        candidates.append(f'learned rule — {rule}')
    try:
        incidents = playbook_core.read_incidents(
            playbook_core.journal_path(STATE_DIR, _SESSION_ID, agent_id=_AGENT_ID), limit=60)
    except Exception:
        incidents = []
    for incident in incidents:
        if incident.get('kind') not in ('repeat_failure', 'correction', 'review_finding'):
            continue
        detail = incident.get('error') or incident.get('prompt') or ''
        if detail:
            candidates.append(f'{incident.get("kind")} — {detail}')

    hits: list[str] = []
    for candidate in candidates:
        lowered = candidate.lower()
        if not any(target.lower() in lowered for target in targets):
            continue
        # A stable digest, not hash(): Python randomises string hashing per
        # process and every hook call is a new process, so hash() would produce a
        # different key each time and the warning would repeat every single turn.
        key = hashlib.sha1(candidate.encode('utf-8', 'replace')).hexdigest()[:12]
        if key in seen:
            continue
        seen.append(key)
        hits.append(candidate[:WATCH_MAX_CHARS])
        if len(hits) >= WATCH_MAX_HITS:
            break

    state['watch_seen'] = seen[-60:]
    if not hits:
        return None
    return {
        'block': False,
        'to_model': True,  # the model is the one about to repeat the failure
        'msg': '[playbook:watch] This project has already recorded:\n'
               + '\n'.join(f'- {hit}' for hit in hits)
               + '\nCheck the action against these before running it.',
    }


def _check_push_gate(state: dict, transcript: str | None, command: str) -> dict | None:
    """Deny a push whose HEAD has not been reviewed.

    Pinned to the commit rather than to the session: reviewing, then committing
    more, then pushing would otherwise publish unreviewed code under a review
    that never saw it. The bypass is the user's own words in their latest
    message (or PLAYBOOK_SKIP_REVIEW in the environment) — never the model's,
    which would let it clear its own gate by writing the phrase.

    Fail-open on everything it cannot establish: no git, no HEAD, nothing ahead
    of upstream."""
    if PUSH_NOOP_RE.search(command):
        return None
    if os.environ.get('PLAYBOOK_SKIP_REVIEW'):
        return None
    # A push that only deletes refs publishes no code, so there is nothing to
    # review — denying `git push origin --delete <merged branch>` until HEAD
    # was reviewed was a measured false positive. Any push whose revisions are
    # unknown still goes through the checks below.
    targets = playbook_core.push_targets(command, _CWD)
    if targets and all(t.revs == [] for t in targets):
        return None

    head = _git_head()
    if not head:
        return None
    if state.get('reviewed_head') == head:
        return None
    if state.get('review_bypass_head') == head:
        return None
    if REVIEW_BYPASS_RE.search(_latest_user_prompt(transcript)):
        # Remembered per commit so the rest of this push (and a retry of it)
        # does not re-prompt for a waiver the user already gave.
        state['review_bypass_head'] = head
        return None
    if _nothing_to_push():
        return None

    return {
        'block': True,
        'msg': (
            f'[playbook:review-gate] Push denied — HEAD {head[:7]} has not been reviewed.\n'
            f'Load the Skill `engineering-code-review` and review `git diff {_push_range()}` '
            'before pushing. Report findings as Blocker / Major / Minor and fix every Blocker.\n'
            'Minimum coverage: information leakage (logs, responses, error payloads), N+1, '
            'idempotency, pagination done in memory, atomicity and transaction boundaries, '
            'dead code, algorithmic complexity, functionality that already exists and was not '
            'reused, leftover console/print debugging, missing explicit types.\n'
            'Bypass: the user says so explicitly (e.g. "pode pushar sem review"), '
            'or PLAYBOOK_SKIP_REVIEW=1 in the environment. You cannot waive this yourself.'
        ),
    }


def _check_learn_gate(state: dict, transcript: str | None) -> dict | None:
    """Block the turn end until a correction has been distilled into a rule.

    Armed by learn_guard.py (a user message rejecting the previous turn, or the
    same call failing repeatedly) and cleared by LEARNED_TOKEN in this turn's
    text. Capped at MAX_LEARN_BLOCKS per session by learn_guard, so a
    false-positive arming costs one turn, not a session."""
    if not state.get('learn_pending'):
        return None
    if _token_after_last_prompt(transcript, LEARNED_TOKEN):
        state['learn_pending'] = False
        state.pop('learn_reason', None)
        return None
    state['learn_blocks'] = int(state.get('learn_blocks', 0)) + 1
    return {
        'block': True,
        'msg': state.get('learn_reason') or (
            f'[playbook:learn] A correction this turn was not distilled into a rule. '
            f'State the wrong premise and what is actually true, persist the rule in the '
            f'project CLAUDE.md, and emit {LEARNED_TOKEN}. '
            f'False positive? Emit `{LEARNED_TOKEN} none — <reason>`.'
        ),
    }


def _check_spec_gate(transcript: str | None) -> dict | None:
    """Block unless SPEC_TOKEN appears in assistant text emitted after the most
    recent real user message — i.e. the model confirmed the spec in the current
    turn, after the user's confirmation. Tool_use entries between the token and
    this call (multi-Write turns) do not invalidate it.

    Evaluated at the STOP hook, not at PreToolUse: the token lives in assistant
    TEXT emitted in the same turn as the guarded write, and the turn's message is
    not flushed to the transcript until its tool calls resolve. A PreToolUse read
    therefore cannot see a token the model just emitted (measured: the majority
    of pre-write spec denials were this false positive). At Stop the text is
    flushed, so this check fires only for genuinely unconfirmed writes."""
    if _token_after_last_prompt(transcript, SPEC_TOKEN):
        return None
    return {
        'block': True,
        'msg': (
            f'[playbook:spec-gate] {SPEC_TOKEN} not found after the latest user message. '
            'Emit it in your response text before the first Write or Edit of this task, '
            "and repeat it in the turn's final message. "
            'For unambiguous single-line fixes: [SPEC CONFIRMED] — unambiguous single-line fix.'
        ),
    }


# ─── Output ───────────────────────────────────────────────────────────────────

def _emit_pretooluse(result: dict) -> None:
    if result.get('block'):
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': result['msg'],
            },
            'systemMessage': result['msg'],
        }))
    elif result.get('to_model'):
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'additionalContext': result['msg'],
            },
            'systemMessage': result['msg'],
        }))
    else:
        print(json.dumps({'systemMessage': result['msg']}))

# ─── Event handlers ───────────────────────────────────────────────────────────

def _turn_assistant_text(transcript: str | None) -> str:
    """Assistant text emitted after the most recent real user message."""
    if not transcript or not os.path.exists(transcript):
        return ''
    parts: list[str] = []
    for line in reversed(_read_tail_lines(transcript)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if _is_user_prompt(obj):
            break
        text = _assistant_text(obj)
        if text:
            parts.append(text)
    return '\n'.join(reversed(parts))


def _capture_handoff(transcript: str | None) -> None:
    """Save the last handoff block the model wrote this turn, if any."""
    if _AGENT_ID:
        return  # a subagent hands its result to the parent, not to a /clear
    try:
        blocks = HANDOFF_RE.findall(_turn_assistant_text(transcript))
        if blocks and blocks[-1].strip():
            import compact_guard
            compact_guard.write_handoff(_CWD or os.getcwd(), blocks[-1])
    except Exception:
        pass  # a handoff write must never block a turn from ending


def _refresh_carryover(state: dict, transcript: str | None) -> None:
    """Keep the carry-over note current once the session is in compaction
    territory.

    PreCompact already writes it, but that only covers a compaction. A user who
    escapes a saturated session with /clear never fires PreCompact, and that is
    exactly the path the context guard recommends -- so the note is refreshed at
    every turn end above the warn threshold."""
    if _AGENT_ID:
        return  # a subagent's context is discarded at its stop; nothing to carry
    try:
        measure = _measure_context(transcript)
        if not measure or measure[0] < WARN_PCT:
            return
        import compact_guard
        compact_guard.write_note(_SESSION_ID, _AGENT_ID, transcript, state,
                                 _CWD or os.getcwd(), 'turn end')
    except Exception:
        pass  # a carry-over write must never block a turn from ending


# A blocked Stop is not free. The response the model has just written becomes an
# intermediate message, and a client that renders only the turn's FINAL message
# drops it silently -- the user waits for a report that was written and never
# shown. Twice observed in one session. The hook cannot know which client is
# rendering, so it states what may have happened and lets the model restate.
_TURN_LOST_NOTE = (
    ' Note: the response you just wrote may never reach the user -- some clients '
    "render only the turn's final message. Restate anything it contained that the "
    'user still needs.'
)


# The pending flag each Stop gate reads. A gate blocks a turn at most once: on
# the continuation Stop (stop_hook_active) the flags of the gates that already
# blocked are cleared and the others still run. Clearing every flag there let
# a spec block swallow a pending validation that never got its turn.
_STOP_GATE_FLAGS = {'spec': 'spec_pending', 'validate': 'pending_validation',
                    'learn': 'learn_pending'}


def _block_turn(state: dict, reason: str, gate: str) -> None:
    """Persist state, block the turn, exit. Single exit point for every Stop gate,
    so the note above cannot be attached to one gate's message and forgotten in the
    next. `gate` is recorded so the continuation Stop clears only its flag."""
    state['stop_blocked'] = [*state.get('stop_blocked', []), gate]
    _save_state(state)
    print(json.dumps({'decision': 'block', 'reason': reason + _TURN_LOST_NOTE}))
    sys.exit(0)


def _handle_stop(input_data: dict, state: dict, transcript: str | None,
                 spec_transcript: str | None) -> None:
    # Suspended via /playbook-off → no gate at all.
    if _scan_skills(transcript, state):
        state['pending_validation'] = False
        state['spec_pending'] = False
        state['learn_pending'] = False
        _save_state(state)
        sys.exit(0)

    # Before any gate can exit: a turn blocked by a gate below ends on a Stop that
    # carries stop_hook_active, which returns early -- the handoff written in the
    # blocked response would never be saved.
    _capture_handoff(transcript)
    _refresh_carryover(state, transcript)

    # A continuation after a block: the gates that already blocked this turn are
    # acknowledged (never loop); the ones that have not still run below.
    if input_data.get('stop_hook_active'):
        for gate in state.get('stop_blocked', []):
            flag = _STOP_GATE_FLAGS.get(gate)
            if flag:
                state[flag] = False
    else:
        state['stop_blocked'] = []

    # Spec gate, deferred from PreToolUse. Code was written this turn without a
    # visible [SPEC CONFIRMED]; the turn's text is now flushed, so this is the
    # first flush-safe point to verify it. Fires only for genuinely unconfirmed
    # writes — the same-turn flush lag that caused false pre-write denials is
    # gone because the token, if emitted, is now on disk.
    if state.get('spec_pending'):
        if _check_spec_gate(spec_transcript) is None:
            state['spec_pending'] = False   # token confirmed once the text flushed
        else:
            msg = (
                '[playbook:spec-gate] Source code was written this turn without '
                f'{SPEC_TOKEN} in your response text, or the text that held it never '
                'reached the transcript. Emit it in your final message '
                f'(for unambiguous single-line fixes: {SPEC_TOKEN} — unambiguous '
                'single-line fix) and state what you implemented and why.'
            )
            _block_turn(state, msg, 'spec')

    if state.get('pending_validation') and not _AGENT_ID and _validated_by_subagent(state):
        state['pending_validation'] = False   # delegated, e.g. to the runner agent

    if state.get('pending_validation'):
        msg = (
            '[playbook:validate] Source files were written this session but no validation '
            "command ran afterwards. Run this project's own type check / lint / test command "
            '(whatever its toolchain provides) per the validation checklist in '
            'engineering-governance, or report what is blocking it.'
        )
        _block_turn(state, msg, 'validate')

    _arm_review_learning(state, spec_transcript)
    learn = _check_learn_gate(state, spec_transcript)
    if learn:
        _block_turn(state, learn['msg'], 'learn')

    memory = _check_memory_budget(state, playbook_core.load_prefs(STATE_DIR))
    if memory:
        _block_turn(state, memory['msg'], 'memory')

    _save_state(state)
    sys.exit(0)


def _handle_pretooluse(input_data: dict, state: dict, transcript: str | None) -> None:
    tool_name  = input_data.get('tool_name', '')
    tool_input = input_data.get('tool_input', {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = tool_input.get('command', '')

    prefs = playbook_core.load_prefs(STATE_DIR)

    # /playbook-off suspends the ENTIRE hook — context lock, gates,
    # destructive-command and commit-attribution blocks — until
    # /playbook-on. Scanned on every tool call (not only code
    # writes), so a suspend takes effect on the very next call, including Agent
    # spawns, Read, and Bash. "Off" means off — full pass-through.
    if _scan_skills(transcript, state):
        _save_state(state)
        sys.exit(0)

    if _AGENT_ID and _is_validation(tool_name, command):
        _note_validation_start(state, str(input_data.get('tool_use_id') or ''))

    _note_memory_write(state, tool_name, tool_input)

    measure = _measure_context(transcript)
    pct   = measure[0] if measure else None
    limit = measure[1] if measure else CONTEXT_LIMIT

    # A context WARNING is advisory (block=False) and must not stand in for a
    # gate verdict. `or`-ing it straight into `result` silenced every gate below
    # it -- destructive, commit attribution, push review, watch, skill -- for the
    # whole remainder of any session that crossed WARN_PCT, because the warning
    # re-fires on every call from then on. A lock (block=True) does short-circuit
    # them, and should: nothing may run at all.
    notice = _check_lock(state, pct) or _check_context(pct, state, limit)
    if not notice:
        notice = _check_budget(int(pct * limit) if pct is not None else None, state, prefs)
    result = notice if notice and notice.get('block') else None

    if not result and tool_name == 'Bash':
        # The authorization lookup reads and parses the transcript tail, so it
        # runs only once the pattern match has already fired — not on every
        # Bash call, the vast majority of which are not destructive at all.
        # What a heredoc writes is data, not commands about to run: a commit
        # message that merely mentions a push, or a doc that documents a
        # dangerous command, must not trip gates that inspect commands.
        # An inline `python3 -c '...'` payload is the same case one surface
        # further: foreign source, not shell about to run.
        # Commit attribution is the exception and gets the RAW command: a
        # script in the same command is often what writes the message.
        runnable = playbook_core.strip_inline_payloads(
            playbook_core.strip_heredoc_bodies(command))
        destructive = _check_destructive(runnable, command, _CWD,
                                         disposable_root=_session_scratchpad())
        if destructive and _destructive_authorized(transcript):
            destructive = None
        is_push = playbook_core.is_git_push(runnable)
        # Attribution and secrets before review: a push carrying either is
        # denied whether or not it was reviewed, and the review would not fix it.
        # The secret write check reads the RAW command — a heredoc body is what
        # lands in the file; commit and push read the history, not the text.
        result = (destructive
                  or playbook_core.check_lab_scanner(runnable, command, _CWD)
                  or _check_ai_attribution(command, _CWD)
                  or playbook_core.check_secret_bash(command, _CWD)
                  or playbook_core.check_commit_secrets(runnable, _CWD)
                  or (playbook_core.check_push_attribution(runnable, _CWD)
                      if is_push else None)
                  or (playbook_core.check_push_secrets(runnable, _CWD)
                      if is_push else None)
                  or (_check_push_gate(state, transcript, runnable)
                      if is_push else None))

    # Only a deny takes the verdict slot here: a "possible secret" advisory in
    # `result` would skip the skill gate below. It is merged in at the end.
    secret_notice = None
    if not result and tool_name in playbook_core.SECRET_WRITE_TOOLS:
        secret = playbook_core.check_secret_write(tool_name, tool_input, _CWD)
        if secret and secret.get('block'):
            result = secret
        else:
            secret_notice = secret

    if not result:
        result = _check_watch(state, prefs, tool_name, tool_input)

    # Extension AND destination: a write counts only when it lands somewhere the
    # project KEEPS. Two measured false positives this closes — a measuring probe
    # written into the repo's git-ignored `scratch/`, and a throwaway trace parser in
    # the session scratchpad under /tmp. Neither is project source, and both used to
    # arm the spec and validation gates. `is_throwaway_write` fails CLOSED: an
    # unresolvable path still counts as source.
    code_write = playbook_core.is_code_write(
        tool_name, tool_input,
        playbook_core.strip_heredoc_bodies(command) if tool_name == 'Bash' else '',
        _CWD or os.getcwd(),
    )

    if not result and code_write:
        # Governance must be loaded before any code write — this signal is a
        # resolved Skill tool call, flushed before the write, so it is safe to
        # enforce here (hard pre-write deny).
        result = _check_skill_gate(state)

    # The advisory outranks every other non-blocking message (a watch hint):
    # governance names this hook the single source of truth for context health.
    # Only a real gate verdict displaces it.
    if notice and not (result and result.get('block')):
        result = notice

    if secret_notice and not (result and result.get('block')):
        result = (secret_notice if not result else
                  {**secret_notice, 'msg': result['msg'] + '\n\n' + secret_notice['msg']})

    _save_state(state)

    if result:
        _emit_pretooluse(result)
    sys.exit(0)


def _is_tool_error(response: object) -> bool:
    """Whether a PostToolUse payload reports the tool as having failed.

    Fails OPEN towards arming: an unrecognized response shape counts as success,
    because a write whose outcome cannot be read is more safely treated as having
    landed than as never having happened."""
    if not isinstance(response, dict):
        return False
    if response.get('success') is False or response.get('is_error') is True:
        return True
    return bool(response.get('error') or response.get('errorMessage'))


def _handle_posttooluse(input_data: dict, state: dict, transcript: str | None) -> None:
    """Arm the deferred gates on the writes that ACTUALLY happened.

    PreToolUse runs before the tool does, so it cannot know the outcome. A write
    denied by a layer this plugin does not control -- the permission prompt, the
    harness's own safety classifier, a tool error -- still reaches PreToolUse, and
    arming there left the session demanding a spec token and a validation run for
    zero bytes on disk. Measured: a denied Edit armed the validation gate, which
    then blocked the end of a turn in which nothing had been written. PostToolUse
    fires only once the tool has completed, so arming here tracks the disk.

    The skill gate stays at PreToolUse, where it belongs: it is the one check that
    must deny the write rather than observe it."""
    # "Off means off" is the module's invariant and PreToolUse enforces it the
    # same way: while suspended this hook writes no gate state at all.
    if _scan_skills(transcript, state):
        sys.exit(0)

    tool_name = str(input_data.get('tool_name') or '')
    tool_input = input_data.get('tool_input') or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = str(tool_input.get('command') or '')

    # Checked before the error exit: a rebase or cherry-pick that stops on a
    # conflict reports failure but may already have written commits.
    if tool_name == 'Bash':
        attribution = playbook_core.check_new_commit_attribution(command, _CWD)
        if attribution:
            print(json.dumps({'decision': 'block', 'reason': attribution,
                              'systemMessage': attribution}))

    if _is_tool_error(input_data.get('tool_response')):
        sys.exit(0)

    # A subagent's Skill call is written to its own transcript, which no tool
    # event carries: the scan above reads the parent's, so a subagent that
    # loaded governance itself stayed denied for good. Its completed call is
    # the one place that load is visible.
    if tool_name == 'Skill':
        loaded = str(tool_input.get('skill') or tool_input.get('command') or '')
        if loaded.rsplit(':', 1)[-1] == GOVERNANCE_SKILL:
            state['gov_loaded'] = True

    if playbook_core.is_code_write(
            tool_name, tool_input,
            playbook_core.strip_heredoc_bodies(command) if tool_name == 'Bash' else '',
            _CWD or os.getcwd()):
        state['pending_validation'] = True
        state['pending_validation_at'] = time.time()
        # The spec token is same-turn assistant TEXT and is not yet flushed to the
        # transcript when this hook fires -- here exactly as at PreToolUse -- so it
        # cannot be verified now without a guaranteed false positive. Record that
        # confirmation is still pending and defer the gate to Stop, where the text
        # is flushed. See _check_spec_gate. A subagent's token lives in its own
        # transcript, which this event does not carry (only SubagentStop does), so
        # its check always defers to SubagentStop.
        state['spec_pending'] = bool(_AGENT_ID) or _check_spec_gate(transcript) is not None

    # A validation command counts once it has actually run. A subagent's also
    # records when it started, for the parent's Stop (_validated_by_subagent).
    if _is_validation(tool_name, command):
        state['pending_validation'] = False
        if _AGENT_ID:
            _note_validation_done(state, str(input_data.get('tool_use_id') or ''))

    _save_state(state)
    sys.exit(0)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    global _SESSION_ID, _AGENT_ID, _CWD
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(input_data, dict):
        sys.exit(0)

    _SESSION_ID = str(input_data.get('session_id') or '')
    _AGENT_ID = str(input_data.get('agent_id') or '')
    _CWD = str(input_data.get('cwd') or os.getcwd())
    transcript = input_data.get('transcript_path') or _latest_jsonl()
    event = input_data.get('hook_event_name') or 'PreToolUse'
    # hooks/hooks.json gives PreToolUse 20 s and the other events 10 s.
    playbook_core.start_hook_clock(15.0 if event == 'PreToolUse' else 8.0)

    state = _load_state()
    if state.get('session_file') != transcript:
        state = {'session_file': transcript, 'locked': False}

    if event in ('Stop', 'SubagentStop'):
        spec_transcript = input_data.get('agent_transcript_path') if _AGENT_ID else transcript
        _handle_stop(input_data, state, transcript, spec_transcript)
    elif event == 'PostToolUse':
        _handle_posttooluse(input_data, state, transcript)
    else:
        _handle_pretooluse(input_data, state, transcript)


if __name__ == '__main__':
    main()
