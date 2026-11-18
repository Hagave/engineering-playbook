"""
Local observability store for playbook development: one SQLite file, outside
every repository, rebuilt from Claude Code session transcripts.

The file holds prompts and commands from private projects, so it never lives
in a checkout: `PLAYBOOK_OBSERVE_DB`, or
`${XDG_STATE_HOME:-~/.local/state}/engineering-playbook/observe.db`, created
readable by its owner only.
"""
import os
import sqlite3

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- How far each transcript has been read: ingestion is incremental.
CREATE TABLE IF NOT EXISTS sources (
    path TEXT PRIMARY KEY,
    offset INTEGER NOT NULL
);

-- agent_id is '' for the main thread of a session.
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    account TEXT NOT NULL,
    project TEXT NOT NULL,
    cwd TEXT,
    first_ts REAL,
    last_ts REAL,
    PRIMARY KEY (session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS prompts (
    uuid TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    chars INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prompts_session ON prompts (session_id, agent_id, ts);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    model TEXT,
    effort TEXT
);

-- One row per API response (message.id); usage repeats on every content
-- block of a response, so it is stored once here.
CREATE TABLE IF NOT EXISTS api_calls (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    request_id TEXT,
    model TEXT,
    effort TEXT,
    input_tokens INTEGER,
    cache_read INTEGER,
    cache_write INTEGER,
    output_tokens INTEGER,
    thinking_tokens INTEGER
);

-- One row per content block of a response: thinking and tool-input streaming
-- time are recorded per block, and the last block's ts ends the call.
CREATE TABLE IF NOT EXISTS api_blocks (
    uuid TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    ts REAL NOT NULL,
    thinking_ms INTEGER NOT NULL,
    stream_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS api_blocks_message ON api_blocks (message_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    session_id TEXT,
    agent_id TEXT,
    message_id TEXT,
    ts_call REAL,
    ts_result REAL,
    tool TEXT,
    kind TEXT,
    path TEXT,
    command TEXT,
    -- Bash only: which check the command ran (see shell.validation_key),
    -- decided from the full command before it is scrubbed and cut.
    validation TEXT,
    is_error INTEGER,
    exit_code INTEGER,
    error_head TEXT,
    denial TEXT,
    permission TEXT,
    result_chars INTEGER,
    -- Bash only: the end of the output, where a test or build prints its
    -- verdict. A command piped into `tail` exits 0 whatever the check said.
    result_tail TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_session ON tool_calls (session_id, agent_id, ts_call);

-- A file view. `full` is 1 when the whole file was shown; start/end are NULL
-- when the range is not known (a grep over the file).
CREATE TABLE IF NOT EXISTS reads (
    tool_use_id TEXT NOT NULL,
    path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    full INTEGER NOT NULL,
    via TEXT NOT NULL,
    PRIMARY KEY (tool_use_id, path, via)
);
CREATE INDEX IF NOT EXISTS reads_path ON reads (path);

-- One row per changed hunk. old_* is the range replaced, new_* the range
-- written, both in the file's line numbers at the time of that edit.
CREATE TABLE IF NOT EXISTS edits (
    tool_use_id TEXT NOT NULL,
    path TEXT NOT NULL,
    hunk INTEGER NOT NULL,
    old_start INTEGER NOT NULL,
    old_lines INTEGER NOT NULL,
    new_start INTEGER NOT NULL,
    new_lines INTEGER NOT NULL,
    via TEXT NOT NULL,
    -- What the hunk changed (see changes.py), and the lines it added split
    -- into code and comment-only.
    change TEXT NOT NULL,
    tokens_changed INTEGER NOT NULL,
    added_code INTEGER NOT NULL,
    added_comment INTEGER NOT NULL,
    PRIMARY KEY (tool_use_id, path, hunk)
);
CREATE INDEX IF NOT EXISTS edits_path ON edits (path);

-- What every hook returned, the playbook's and anyone else's.
CREATE TABLE IF NOT EXISTS hook_events (
    uuid TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    ts REAL NOT NULL,
    event TEXT,
    hook_name TEXT,
    tool_use_id TEXT,
    outcome TEXT NOT NULL,
    command TEXT,
    duration_ms INTEGER,
    exit_code INTEGER,
    gate TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS hook_events_session ON hook_events (session_id, agent_id, ts);

-- Transcript shapes the ingester did not recognise. Never silently dropped:
-- the report prints this table.
CREATE TABLE IF NOT EXISTS unknown_records (
    record_type TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);

-- Derived by episodes.py, rebuilt on every derive: every project file a
-- session edited, with what the model had seen of it before the first edit.
-- The denominator for "does rework follow from not having read the file".
CREATE TABLE IF NOT EXISTS edited_files (
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    path TEXT NOT NULL,
    target_read TEXT NOT NULL,
    hunks INTEGER NOT NULL,
    rewritten_hunks INTEGER NOT NULL,
    PRIMARY KEY (session_id, agent_id, path)
);

-- Derived by episodes.py, rebuilt on every derive: every hunk that rewrote
-- lines the model had written earlier in the same request, with what happened
-- between the two (see episodes.TRIGGERS). What it changed is in `edits`.
CREATE TABLE IF NOT EXISTS rewrites (
    tool_use_id TEXT NOT NULL,
    path TEXT NOT NULL,
    hunk INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    PRIMARY KEY (tool_use_id, path, hunk)
);

-- Derived by episodes.py, rebuilt on every derive.
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts_start REAL NOT NULL,
    ts_end REAL NOT NULL,
    validation TEXT,
    attempts INTEGER NOT NULL,
    resolved INTEGER NOT NULL,
    error_class TEXT,
    cause TEXT NOT NULL,
    target_read TEXT NOT NULL,
    fix_edits INTEGER NOT NULL,
    details TEXT NOT NULL
);
"""


class SchemaMismatch(RuntimeError):
    """The file was written by another schema version."""


def default_path() -> str:
    explicit = os.environ.get('PLAYBOOK_OBSERVE_DB')
    if explicit:
        return explicit
    state_home = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(state_home, 'engineering-playbook', 'observe.db')


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the store, creating it and its schema on first use."""
    path = path or default_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fresh = not os.path.exists(path)
    if fresh:
        # Created empty with the final mode, so the data is never world-readable.
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()
    elif row['value'] != str(SCHEMA_VERSION):
        conn.close()
        raise SchemaMismatch(
            f'{path} has schema {row["value"]}, this tool writes {SCHEMA_VERSION}. '
            'The store is rebuilt from the transcripts: delete the file and ingest again.')
    return conn
