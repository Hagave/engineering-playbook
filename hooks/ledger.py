#!/usr/bin/env python3
"""
Engineering Playbook — continuity ledger (record only).

Each hook event it is registered for writes what the payload says happened
into one SQLite store per repository: the prompts submitted, the answers to
AskUserQuestion, the files read and written (path and content hash, never the
content), the validation runs (outcome and repository state), each turn's
closing message, and the session chain across /clear. Fed only from the hook
payload on stdin, never from the transcript. Nothing reads it back into a
session yet.

The store lives outside every repository: `$PLAYBOOK_LEDGER_DIR`, or
`${XDG_STATE_HOME:-~/.local/state}/engineering-playbook/ledger/`, one file per
git common directory, so the worktrees of a repository share it and every row
names its worktree. A directory outside any repository gets a store of its
own, keyed by its path.

A record, never a gate: it prints nothing, always exits 0, and a write that
fails is journaled in the session's failure journal, once per distinct error.
"""
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCHEMA_VERSION = 1
STATE_DIR = os.path.expanduser('~/.claude/hooks')

# SessionEnd hooks share a 1.5 s budget that a plugin's own timeout does not
# raise, so locating the store and waiting for the lock must fit inside it.
GIT_LOCATE_TIMEOUT_S = 1.0
BUSY_TIMEOUT_S = 0.5
REPO_STATE_BUDGET_S = 1.5
SECRET_SCAN_BUDGET_S = 0.5

# Prompts are kept verbatim by the owner's decision; past this size (a pasted
# file) the tail is cut and `chars` keeps the real length.
MAX_TEXT_CHARS = 256_000
MAX_JSON_CHARS = 32_000
MAX_COMMAND_CHARS = 4_000
MAX_HASH_BYTES = 16 * 1024 * 1024
MAX_UNTRACKED_BYTES = 32 * 1024 * 1024

# The cleared session and the one that replaces it share no id in any payload
# (probe: SessionEnd reason=clear, then SessionStart source=clear 0.1 s later).
# The claim takes the newest open marker of the worktree inside this window;
# CLAUDE_PID is recorded on both sides to measure whether it can be the link.
CLAIM_WINDOW_S = 120.0

FILE_TOOLS = {
    'Read': ('read', 'file_path'),
    'Edit': ('write', 'file_path'),
    'Write': ('write', 'file_path'),
    'NotebookEdit': ('write', 'notebook_path'),
}
ANSWER_TOOL = 'AskUserQuestion'
_EXIT_CODE_RE = re.compile(r'Exit code (\d+)')

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        worktree TEXT NOT NULL,
        started_at REAL,
        source TEXT,
        last_started_at REAL,
        last_source TEXT,
        model TEXT,
        host_pid INTEGER,
        inherited_from TEXT,
        ended_at REAL,
        end_reason TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS handoffs (
        from_session TEXT PRIMARY KEY,
        worktree TEXT NOT NULL,
        created_at REAL NOT NULL,
        host_pid INTEGER,
        claimed_by TEXT UNIQUE,
        claimed_at REAL,
        pid_match INTEGER
    )''',
    'CREATE INDEX IF NOT EXISTS handoffs_open ON handoffs (worktree, created_at) WHERE claimed_by IS NULL',
    '''CREATE TABLE IF NOT EXISTS prompts (
        session_id TEXT NOT NULL,
        prompt_id TEXT NOT NULL,
        text_sha256 TEXT NOT NULL,
        worktree TEXT NOT NULL,
        ts REAL NOT NULL,
        permission_mode TEXT,
        chars INTEGER NOT NULL,
        text TEXT NOT NULL,
        redacted INTEGER,
        expansion_type TEXT,
        command_name TEXT,
        command_args TEXT,
        command_source TEXT,
        PRIMARY KEY (session_id, prompt_id, text_sha256)
    )''',
    'CREATE INDEX IF NOT EXISTS prompts_session ON prompts (session_id, ts)',
    '''CREATE TABLE IF NOT EXISTS answers (
        tool_use_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        prompt_id TEXT,
        worktree TEXT NOT NULL,
        ts REAL NOT NULL,
        tool_input TEXT NOT NULL,
        tool_response TEXT NOT NULL,
        redacted INTEGER
    )''',
    'CREATE INDEX IF NOT EXISTS answers_session ON answers (session_id, ts)',
    '''CREATE TABLE IF NOT EXISTS files (
        tool_use_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        agent_type TEXT,
        prompt_id TEXT,
        worktree TEXT NOT NULL,
        ts REAL NOT NULL,
        tool TEXT NOT NULL,
        action TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT,
        size INTEGER,
        start_line INTEGER,
        num_lines INTEGER,
        total_lines INTEGER,
        write_type TEXT,
        user_modified INTEGER
    )''',
    'CREATE INDEX IF NOT EXISTS files_session ON files (session_id, ts)',
    'CREATE INDEX IF NOT EXISTS files_path ON files (path, ts)',
    '''CREATE TABLE IF NOT EXISTS runs (
        tool_use_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        agent_type TEXT,
        prompt_id TEXT,
        worktree TEXT NOT NULL,
        ts REAL NOT NULL,
        command TEXT NOT NULL,
        outcome TEXT NOT NULL,
        exit_code INTEGER,
        interrupted INTEGER,
        duration_ms INTEGER,
        background_task_id TEXT,
        head TEXT,
        tree_sha256 TEXT,
        state_error TEXT
    )''',
    'CREATE INDEX IF NOT EXISTS runs_session ON runs (session_id, ts)',
    '''CREATE TABLE IF NOT EXISTS turns (
        session_id TEXT NOT NULL,
        prompt_id TEXT NOT NULL,
        message_sha256 TEXT NOT NULL,
        worktree TEXT NOT NULL,
        ts REAL NOT NULL,
        permission_mode TEXT,
        stop_hook_active INTEGER,
        last_message TEXT NOT NULL,
        redacted INTEGER,
        background_tasks TEXT,
        session_crons TEXT,
        PRIMARY KEY (session_id, prompt_id, message_sha256)
    )''',
    'CREATE INDEX IF NOT EXISTS turns_session ON turns (session_id, ts)',
)


class SchemaMismatch(RuntimeError):
    """The store was written by another schema version."""


class Location(NamedTuple):
    project: str
    worktree: str
    in_git: bool


class RepoState(NamedTuple):
    head: str | None
    tree_sha256: str | None
    error: str | None


def ledger_dir() -> str:
    explicit = os.environ.get('PLAYBOOK_LEDGER_DIR')
    if explicit:
        return explicit
    state_home = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(state_home, 'engineering-playbook', 'ledger')


def _git_env() -> dict[str, str]:
    # Read-only git: no opportunistic index refresh written into the project.
    return {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}


def locate(cwd: str) -> Location:
    """The repository `cwd` belongs to (its git common directory, shared by its
    worktrees) and the worktree itself; outside a repository, `cwd` for both."""
    try:
        done = subprocess.run(
            ('git', '-C', cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir', '--show-toplevel'),
            capture_output=True, text=True, timeout=GIT_LOCATE_TIMEOUT_S, env=_git_env())
    except FileNotFoundError:
        done = None
    lines = done.stdout.splitlines() if done is not None and done.returncode == 0 else []
    if len(lines) == 2:
        return Location(os.path.realpath(lines[0]), os.path.realpath(lines[1]), True)
    real = os.path.realpath(cwd)
    return Location(real, real, False)


def store_path(location: Location) -> str:
    kind = 'git' if location.in_git else 'dir'
    key = hashlib.sha256(f'{kind}:{location.project}'.encode()).hexdigest()[:16]
    return os.path.join(ledger_dir(), f'{key}.db')


def _schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError as error:
        if 'no such table' in str(error):
            return None
        raise
    return row[0] if row else None


def _create(path: str, location: Location) -> None:
    """Build the store complete in a private file (mkstemp: owner-only, as it
    holds prompts verbatim) and publish it with link(), which fails if another
    hook published first. Switching a shared file to WAL and creating its
    schema in place raced: a writer arriving meanwhile got "database is
    locked" without the busy timeout ever applying."""
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.creating-', suffix='.db')
    os.close(handle)
    try:
        conn = sqlite3.connect(tmp, isolation_level=None)
        try:
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('BEGIN')
            for statement in SCHEMA:
                conn.execute(statement)
            conn.executemany('INSERT INTO meta (key, value) VALUES (?, ?)', (
                ('schema_version', str(SCHEMA_VERSION)),
                ('project', location.project),
                ('in_git', '1' if location.in_git else '0'),
            ))
            conn.execute('COMMIT')
        finally:
            conn.close()
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(tmp)


def connect(path: str, location: Location) -> sqlite3.Connection:
    """Open the store for writing, creating it and its schema on first use."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if not os.path.exists(path):
        _create(path, location)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    try:
        version = _schema_version(conn)
        if version != str(SCHEMA_VERSION):
            raise SchemaMismatch(f'{path} has schema {version}, this hook writes {SCHEMA_VERSION}')
        # WAL with NORMAL never corrupts the file; a power loss can drop the
        # last commits. A record of a session is worth less than an fsync on
        # every tool call.
        conn.execute('PRAGMA synchronous = NORMAL')
    except BaseException:
        conn.close()
        raise
    return conn


def _opt(value: object) -> str | None:
    return str(value) if value not in (None, '') else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag(value: object) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()


def _host_pid() -> int | None:
    value = os.environ.get('CLAUDE_PID', '')
    return int(value) if value.isdigit() else None


def scrubbed(text: str) -> tuple[str, int | None]:
    """`text` with deny-tier secrets cut, and 1/0 for whether any was, or None
    when the scan could not finish in its budget (the text is kept as is)."""
    import secret_scan
    secret_scan.set_deadline(time.monotonic() + SECRET_SCAN_BUDGET_S)
    found = secret_scan.scan_text(text, tiers=(secret_scan.TIER_DENY,))
    secret_scan.set_deadline(None)
    if found is None:
        return text, None
    if not found:
        return text, 0
    return secret_scan.redact(text), 1


def _json_capped(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_JSON_CHARS else text[:MAX_JSON_CHARS] + f'...[{len(text)} chars]'


def file_digest(path: str) -> tuple[str | None, int | None]:
    """sha256 and size of a regular file as it is on disk now; no hash above
    MAX_HASH_BYTES, nothing for a path that is not a regular file."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return None, None
        if info.st_size > MAX_HASH_BYTES:
            return None, info.st_size
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
        return digest.hexdigest(), info.st_size
    except OSError:
        return None, None


class _NoState(Exception):
    pass


def _git_bytes(worktree: str, deadline: float, *args: str) -> tuple[int, bytes]:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _NoState('timeout')
    try:
        done = subprocess.run(('git', '-C', worktree) + args, capture_output=True,
                              timeout=left, env=_git_env())
    except subprocess.TimeoutExpired:
        raise _NoState('timeout') from None
    except OSError as error:
        raise _NoState(f'git: {error}') from None
    return done.returncode, done.stdout


def repo_state(worktree: str) -> RepoState:
    """HEAD and one hash of the working tree's difference from it: the tracked
    diff plus every untracked, not-ignored file's content."""
    deadline = time.monotonic() + REPO_STATE_BUDGET_S
    try:
        code, out = _git_bytes(worktree, deadline, 'rev-parse', '--verify', '-q', 'HEAD')
        if code != 0:
            return RepoState(None, None, 'no HEAD')
        head = out.decode().strip()
        code, diff = _git_bytes(worktree, deadline, 'diff', 'HEAD', '--binary', '--no-ext-diff',
                                '--no-textconv', '--no-color')
        if code != 0:
            raise _NoState('git diff failed')
        code, listing = _git_bytes(worktree, deadline, 'ls-files', '-z', '--others', '--exclude-standard')
        if code != 0:
            raise _NoState('git ls-files failed')
        digest = hashlib.sha256(diff)
        budget = MAX_UNTRACKED_BYTES
        for name in sorted(filter(None, listing.split(b'\0'))):
            if time.monotonic() > deadline:
                raise _NoState('timeout')
            path = os.path.join(worktree, os.fsdecode(name))
            sha, size = file_digest(path)
            budget -= size or 0
            if budget < 0:
                raise _NoState('untracked files too large')
            digest.update(b'\0' + name + b'\0' + (sha or '?').encode())
        return RepoState(head, digest.hexdigest(), None)
    except _NoState as error:
        return RepoState(None, None, str(error))


def _require(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not value:
        raise ValueError(f'{payload.get("hook_event_name")} payload without {key}')
    return str(value)


def _agent(payload: dict) -> tuple[str, str | None]:
    return str(payload.get('agent_id') or ''), _opt(payload.get('agent_type'))


def _record_session_start(conn: sqlite3.Connection, payload: dict, location: Location, now: float) -> None:
    session_id = _require(payload, 'session_id')
    source = _opt(payload.get('source'))
    host_pid = _host_pid()
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('''
            INSERT INTO sessions (session_id, worktree, started_at, source, last_started_at,
                                  last_source, model, host_pid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                started_at = COALESCE(sessions.started_at, excluded.started_at),
                source = COALESCE(sessions.source, excluded.source),
                last_started_at = excluded.last_started_at,
                last_source = excluded.last_source,
                model = COALESCE(excluded.model, sessions.model),
                host_pid = COALESCE(excluded.host_pid, sessions.host_pid)''',
            (session_id, location.worktree, now, source, now, source,
             _opt(payload.get('model')), host_pid))
        if source == 'clear':
            _claim_handoff(conn, session_id, location.worktree, host_pid, now)
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise


def _claim_handoff(conn: sqlite3.Connection, session_id: str, worktree: str,
                   host_pid: int | None, now: float) -> None:
    if conn.execute('SELECT 1 FROM handoffs WHERE claimed_by = ?', (session_id,)).fetchone():
        return
    row = conn.execute('''
        SELECT from_session, host_pid FROM handoffs
        WHERE worktree = ? AND claimed_by IS NULL AND from_session != ? AND created_at >= ?
        ORDER BY created_at DESC LIMIT 1''', (worktree, session_id, now - CLAIM_WINDOW_S)).fetchone()
    if row is None:
        return
    from_session, marker_pid = row
    pid_match = None if host_pid is None or marker_pid is None else int(host_pid == marker_pid)
    conn.execute('UPDATE handoffs SET claimed_by = ?, claimed_at = ?, pid_match = ? WHERE from_session = ?',
                 (session_id, now, pid_match, from_session))
    conn.execute('UPDATE sessions SET inherited_from = ? WHERE session_id = ?', (from_session, session_id))


def _record_session_end(conn: sqlite3.Connection, payload: dict, location: Location, now: float) -> None:
    session_id = _require(payload, 'session_id')
    reason = _opt(payload.get('reason'))
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('''
            INSERT INTO sessions (session_id, worktree, ended_at, end_reason, host_pid)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                ended_at = excluded.ended_at, end_reason = excluded.end_reason''',
            (session_id, location.worktree, now, reason, _host_pid()))
        if reason == 'clear':
            conn.execute('''
                INSERT INTO handoffs (from_session, worktree, created_at, host_pid) VALUES (?, ?, ?, ?)
                ON CONFLICT (from_session) DO NOTHING''',
                (session_id, location.worktree, now, _host_pid()))
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise


def _record_prompt(conn: sqlite3.Connection, payload: dict, location: Location, now: float) -> None:
    raw = str(payload.get('prompt') or '')
    if not raw:
        return
    text, redacted = scrubbed(raw[:MAX_TEXT_CHARS])
    expansion = payload.get('hook_event_name') == 'UserPromptExpansion'
    args = _opt(payload.get('command_args')) if expansion else None
    conn.execute('''
        INSERT INTO prompts (session_id, prompt_id, text_sha256, worktree, ts, permission_mode, chars,
                             text, redacted, expansion_type, command_name, command_args, command_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id, prompt_id, text_sha256) DO UPDATE SET
            expansion_type = COALESCE(excluded.expansion_type, prompts.expansion_type),
            command_name = COALESCE(excluded.command_name, prompts.command_name),
            command_args = COALESCE(excluded.command_args, prompts.command_args),
            command_source = COALESCE(excluded.command_source, prompts.command_source)''',
        (_require(payload, 'session_id'), str(payload.get('prompt_id') or ''), _sha256(raw),
         location.worktree, now, _opt(payload.get('permission_mode')), len(raw), text, redacted,
         _opt(payload.get('expansion_type')) if expansion else None,
         _opt(payload.get('command_name')) if expansion else None,
         scrubbed(args)[0] if args else None,
         _opt(payload.get('command_source')) if expansion else None))


def _record_file(conn: sqlite3.Connection, payload: dict, location: Location, now: float) -> None:
    tool = str(payload.get('tool_name'))
    action, key = FILE_TOOLS[tool]
    tool_input = _dict(payload.get('tool_input'))
    path = str(tool_input.get(key) or '')
    if not path:
        raise ValueError(f'{tool} payload without tool_input.{key}')
    path = os.path.join(str(payload.get('cwd') or ''), path)
    response = _dict(payload.get('tool_response'))
    shown = _dict(response.get('file')) if tool == 'Read' else {}
    sha, size = file_digest(path)
    agent_id, agent_type = _agent(payload)
    conn.execute('''
        INSERT INTO files (tool_use_id, session_id, agent_id, agent_type, prompt_id, worktree, ts, tool,
                           action, path, sha256, size, start_line, num_lines, total_lines, write_type,
                           user_modified)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tool_use_id) DO NOTHING''',
        (_require(payload, 'tool_use_id'), _require(payload, 'session_id'), agent_id, agent_type,
         _opt(payload.get('prompt_id')), location.worktree, now, tool, action, path, sha, size,
         _int(shown.get('startLine')), _int(shown.get('numLines')), _int(shown.get('totalLines')),
         _opt(response.get('type')) if tool == 'Write' else None,
         _flag(response.get('userModified'))))


def _record_answer(conn: sqlite3.Connection, payload: dict, location: Location, now: float) -> None:
    # The PostToolUse shape of AskUserQuestion was not observed (headless runs
    # cannot ask), so input and response are kept whole and parsed by a reader.
    tool_input, redacted_in = scrubbed(_json_capped(payload.get('tool_input')))
    response, redacted_out = scrubbed(_json_capped(payload.get('tool_response')))
    redacted = None if None in (redacted_in, redacted_out) else max(redacted_in, redacted_out)
    conn.execute('''
        INSERT INTO answers (tool_use_id, session_id, agent_id, prompt_id, worktree, ts, tool_input,
                             tool_response, redacted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tool_use_id) DO NOTHING''',
        (_require(payload, 'tool_use_id'), _require(payload, 'session_id'), _agent(payload)[0],
         _opt(payload.get('prompt_id')), location.worktree, now, tool_input, response, redacted))


def _bash_command(payload: dict) -> str:
    return str(_dict(payload.get('tool_input')).get('command') or '')


def is_validation_run(payload: dict) -> bool:
    if payload.get('tool_name') != 'Bash':
        return False
    import playbook_core
    return playbook_core.is_validation_command(_bash_command(payload))


def _record_run(conn: sqlite3.Connection, payload: dict, location: Location, now: float) -> None:
    tool_use_id = _require(payload, 'tool_use_id')
    session_id = _require(payload, 'session_id')
    tool_input = _dict(payload.get('tool_input'))
    response = _dict(payload.get('tool_response'))
    exit_code = None
    background_task_id = None
    if payload.get('hook_event_name') == 'PostToolUseFailure':
        outcome = 'failed'
        match = _EXIT_CODE_RE.match(str(payload.get('error') or ''))
        exit_code = int(match.group(1)) if match else None
        interrupted = _flag(payload.get('is_interrupt'))
    elif response.get('backgroundTaskId') or tool_input.get('run_in_background'):
        outcome = 'background'
        background_task_id = _opt(response.get('backgroundTaskId'))
        interrupted = None
    else:
        outcome = 'succeeded'
        interrupted = _flag(response.get('interrupted'))
    state = repo_state(location.worktree) if location.in_git else RepoState(None, None, 'not a repository')
    command, _ = scrubbed(_bash_command(payload)[:MAX_COMMAND_CHARS])
    agent_id, agent_type = _agent(payload)
    conn.execute('''
        INSERT INTO runs (tool_use_id, session_id, agent_id, agent_type, prompt_id, worktree, ts, command,
                          outcome, exit_code, interrupted, duration_ms, background_task_id, head,
                          tree_sha256, state_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tool_use_id) DO NOTHING''',
        (tool_use_id, session_id, agent_id, agent_type, _opt(payload.get('prompt_id')), location.worktree,
         now, command, outcome, exit_code, interrupted, _int(payload.get('duration_ms')),
         background_task_id, state.head, state.tree_sha256, state.error))


def _record_turn(conn: sqlite3.Connection, payload: dict, location: Location, now: float) -> None:
    raw = str(payload.get('last_assistant_message') or '')
    text, redacted = scrubbed(raw[:MAX_TEXT_CHARS])
    conn.execute('''
        INSERT INTO turns (session_id, prompt_id, message_sha256, worktree, ts, permission_mode,
                           stop_hook_active, last_message, redacted, background_tasks, session_crons)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id, prompt_id, message_sha256) DO NOTHING''',
        (_require(payload, 'session_id'), str(payload.get('prompt_id') or ''), _sha256(raw),
         location.worktree, now, _opt(payload.get('permission_mode')),
         _flag(payload.get('stop_hook_active')), text, redacted,
         _json_capped(payload['background_tasks']) if 'background_tasks' in payload else None,
         _json_capped(payload['session_crons']) if 'session_crons' in payload else None))


Recorder = Callable[[sqlite3.Connection, dict, Location, float], None]


def recorder_for(payload: dict) -> Recorder | None:
    """What writes this event, or None when it is not recorded (decided before
    the store is opened, so an unrecorded tool call costs no git or SQLite)."""
    event = payload.get('hook_event_name')
    tool = payload.get('tool_name')
    if event == 'PostToolUse':
        if tool in FILE_TOOLS:
            return _record_file
        if tool == ANSWER_TOOL:
            return _record_answer
        return _record_run if is_validation_run(payload) else None
    if event == 'PostToolUseFailure':
        return _record_run if is_validation_run(payload) else None
    return {
        'SessionStart': _record_session_start,
        'SessionEnd': _record_session_end,
        'UserPromptSubmit': _record_prompt,
        'UserPromptExpansion': _record_prompt,
        'Stop': _record_turn,
    }.get(str(event))


def record(payload: dict, now: float | None = None) -> None:
    recorder = recorder_for(payload)
    if recorder is None:
        return
    location = locate(str(payload.get('cwd') or os.getcwd()))
    conn = connect(store_path(location), location)
    try:
        recorder(conn, payload, location, time.time() if now is None else now)
    finally:
        conn.close()


def journal_failure(payload: dict, error: BaseException) -> None:
    """Report a failed write in the session's failure journal, once per
    distinct error: a store that stays broken must not push out the journal's
    real failures (it keeps its last 200 lines)."""
    try:
        import playbook_core
        path = playbook_core.journal_path(STATE_DIR, str(payload.get('session_id') or ''),
                                          str(payload.get('cwd') or '') or None,
                                          str(payload.get('agent_id') or ''))
        message = playbook_core.scrub_secrets(f'{type(error).__name__}: {error}')[:300]
        for incident in playbook_core.read_incidents(path, limit=50):
            if incident.get('kind') == 'ledger_error' and incident.get('error') == message:
                return
        playbook_core.append_incident(path, {
            'ts': time.time(),
            'kind': 'ledger_error',
            'cwd': str(payload.get('cwd') or ''),
            'tool': 'ledger',
            'input': str(payload.get('hook_event_name') or ''),
            'error': message,
        })
    except Exception:  # noqa: BLE001 -- the journal is the last place a failure can go
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError(f'hook payload is a {type(payload).__name__}, not an object')
    except Exception as error:  # noqa: BLE001
        journal_failure({}, error)
        sys.exit(0)
    try:
        record(payload)
    except Exception as error:  # noqa: BLE001 -- a record must never fail the event it records
        journal_failure(payload, error)
    sys.exit(0)


if __name__ == '__main__':
    main()
