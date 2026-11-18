"""
Claude Code session transcripts -> the observability store.

The transcript is the one record that holds everything a session did: every
API call with its usage, effort and thinking time, every tool call with its
result, and what each hook returned. Its format is internal to Claude Code and
changes between versions, so a record type this reader does not know is
counted in `unknown_records`, never dropped in silence, and the store can
always be deleted and rebuilt.

Ingestion is incremental (a byte offset per file) and idempotent (every row is
keyed by an id the transcript carries).
"""
import collections
import glob
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shell  # noqa: E402
from shell import playbook_core  # noqa: E402

MAX_TEXT = 2000
MAX_HEAD = 600
MAX_TAIL = 400
MAX_COMMAND_LABEL = 200

# Bookkeeping records with nothing to measure.
IGNORED_TYPES = frozenset({
    'last-prompt', 'mode', 'permission-mode', 'session-owner', 'atis-latch',
    'file-history-snapshot', 'file-history-delta', 'api-request-shape',
    'api-request-blob', 'ai-title', 'tools-used', 'pr-link', 'cost-state',
    'queue-operation', 'bridge-session', 'frame-link', 'artifact-comment-monitor',
    'artifact-autoreact-ledger', 'continued-in', 'agent-name', 'system', 'summary',
    'fork-context-ref', 'dev-mods', 'agent-setting',
})

_TOOL_KINDS = {
    'Read': 'read', 'Edit': 'edit', 'MultiEdit': 'edit', 'NotebookEdit': 'edit',
    'Write': 'write', 'Bash': 'bash', 'Skill': 'skill', 'Agent': 'agent', 'Task': 'agent',
}
_EXIT_CODE_RE = re.compile(r'^(?:Error: )?Exit code (\d+)')
_GATE_RE = re.compile(r'\[playbook:([\w-]+)\]')
_AGENT_FILE_RE = re.compile(r'agent-([0-9a-f]+)\.jsonl$')


def _ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _clip(text: object, limit: int) -> str:
    """Text as stored: secrets scrubbed, then cut. The store is local, but a
    token pasted into a prompt or a command should not gain a second home."""
    return playbook_core.scrub_secrets(str(text or ''))[:limit]


def _block_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(block.get('text') or '') for block in content
                         if isinstance(block, dict) and block.get('type') == 'text')
    return ''


def transcripts(roots: list[str] | None = None, days: float | None = None) -> list[tuple[str, str, str]]:
    """(path, account, project) of every main and subagent transcript under
    the `projects` directories of the Claude Code accounts on this machine."""
    roots = roots or sorted(glob.glob(os.path.expanduser('~/.claude*/projects')))
    cutoff = time.time() - days * 86400 if days else None
    found: list[tuple[str, str, str]] = []
    for root in roots:
        account = os.path.basename(os.path.dirname(os.path.abspath(root)))
        for project_dir in sorted(glob.glob(os.path.join(root, '*'))):
            project = os.path.basename(project_dir)
            paths = (glob.glob(os.path.join(project_dir, '*.jsonl'))
                     + glob.glob(os.path.join(project_dir, '*', 'subagents', '*.jsonl')))
            for path in sorted(paths):
                if cutoff is None or os.path.getmtime(path) >= cutoff:
                    found.append((path, account, project))
    return found


class Ingester:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.unknown: collections.Counter[str] = collections.Counter()
        self.records = 0

    # ── files ────────────────────────────────────────────────────────────────

    def ingest_file(self, path: str, account: str, project: str) -> int:
        """Read what `path` gained since the last run. Returns records read."""
        row = self.conn.execute('SELECT offset FROM sources WHERE path = ?', (path,)).fetchone()
        offset = row['offset'] if row else 0
        if os.path.getsize(path) < offset:
            offset = 0  # rewritten in place: rows are keyed by id, so a re-read is safe
        match = _AGENT_FILE_RE.search(path)
        default_agent = match.group(1) if match else ''
        spans: dict[tuple[str, str], list] = {}
        read = 0
        with open(path, 'rb') as handle:
            handle.seek(offset)
            for raw in handle:
                if not raw.endswith(b'\n'):
                    break  # a line still being written: next run reads it whole
                offset += len(raw)
                read += 1
                try:
                    obj = json.loads(raw)
                except ValueError:
                    self.unknown['<bad json>'] += 1
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    self._record(obj, default_agent, spans)
                except (AttributeError, KeyError, TypeError, ValueError):
                    # A known record type in a shape this reader was not
                    # written for: count it and keep reading the session.
                    self.unknown[f"{obj.get('type')}: unexpected shape"] += 1
        for (session_id, agent_id), (first, last, cwd) in spans.items():
            self.conn.execute(
                'INSERT INTO sessions (session_id, agent_id, account, project, cwd, first_ts, last_ts) '
                'VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (session_id, agent_id) DO UPDATE SET '
                'first_ts = MIN(sessions.first_ts, excluded.first_ts), '
                'last_ts = MAX(sessions.last_ts, excluded.last_ts), '
                'cwd = COALESCE(sessions.cwd, excluded.cwd)',
                (session_id, agent_id, account, project, cwd, first, last))
        self.conn.execute(
            'INSERT INTO sources (path, offset) VALUES (?, ?) '
            'ON CONFLICT (path) DO UPDATE SET offset = excluded.offset', (path, offset))
        self.conn.commit()
        self.records += read
        return read

    def finish(self) -> None:
        for record_type, count in self.unknown.items():
            self.conn.execute(
                'INSERT INTO unknown_records (record_type, count) VALUES (?, ?) '
                'ON CONFLICT (record_type) DO UPDATE SET count = count + excluded.count',
                (record_type, count))
        self.conn.commit()

    # ── records ──────────────────────────────────────────────────────────────

    def _record(self, obj: dict, default_agent: str, spans: dict) -> None:
        kind = obj.get('type')
        if kind == 'api-request':
            params = obj.get('params') if isinstance(obj.get('params'), dict) else {}
            config = params.get('output_config') if isinstance(params.get('output_config'), dict) else {}
            ts = _ts(obj.get('timestamp'))
            if obj.get('id') and ts is not None:
                self.conn.execute(
                    'INSERT OR IGNORE INTO requests (id, ts, model, effort) VALUES (?, ?, ?, ?)',
                    (obj['id'], ts, params.get('model'), config.get('effort')))
            return
        if kind in IGNORED_TYPES:
            return
        if kind not in ('user', 'assistant', 'attachment'):
            self.unknown[str(kind)] += 1
            return

        ts = _ts(obj.get('timestamp'))
        if ts is None:
            self.unknown[f'{kind}:no timestamp'] += 1
            return
        session_id = str(obj.get('sessionId') or '')
        agent_id = str(obj.get('agentId') or default_agent)
        span = spans.setdefault((session_id, agent_id), [ts, ts, obj.get('cwd')])
        span[0], span[1] = min(span[0], ts), max(span[1], ts)
        cwd = str(obj.get('cwd') or '')

        if kind == 'assistant':
            self._assistant(obj, ts, session_id, agent_id, cwd)
        elif kind == 'user':
            self._user(obj, ts, session_id, agent_id)
        else:
            self._attachment(obj, ts, session_id, agent_id)

    def _assistant(self, obj: dict, ts: float, session_id: str, agent_id: str, cwd: str) -> None:
        message = obj.get('message') if isinstance(obj.get('message'), dict) else {}
        message_id = str(message.get('id') or obj.get('uuid') or '')
        usage = message.get('usage') if isinstance(message.get('usage'), dict) else {}
        details = usage.get('output_tokens_details')
        thinking_tokens = details.get('thinking_tokens') if isinstance(details, dict) else None
        self.conn.execute(
            'INSERT OR REPLACE INTO api_calls (message_id, session_id, agent_id, request_id, model, '
            'effort, input_tokens, cache_read, cache_write, output_tokens, thinking_tokens) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (message_id, session_id, agent_id, obj.get('requestRef'), message.get('model'),
             obj.get('effort'), usage.get('input_tokens'), usage.get('cache_read_input_tokens'),
             usage.get('cache_creation_input_tokens'), usage.get('output_tokens'), thinking_tokens))
        self.conn.execute(
            'INSERT OR IGNORE INTO api_blocks (uuid, message_id, ts, thinking_ms, stream_ms) '
            'VALUES (?, ?, ?, ?, ?)',
            (obj.get('uuid'), message_id, ts, int(obj.get('thinkingDurationMs') or 0),
             int(obj.get('toolInputStreamMs') or 0)))
        content = message.get('content') if isinstance(message.get('content'), list) else []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('id'):
                self._tool_use(block, ts, session_id, agent_id, message_id, cwd)

    def _tool_use(self, block: dict, ts: float, session_id: str, agent_id: str,
                  message_id: str, cwd: str) -> None:
        tool = str(block.get('name') or '')
        tool_input = block.get('input') if isinstance(block.get('input'), dict) else {}
        kind = _TOOL_KINDS.get(tool, 'other')
        path = tool_input.get('file_path') or tool_input.get('notebook_path')
        command = validation = None
        if kind == 'bash':
            raw = str(tool_input.get('command') or '')
            command = _clip(raw, MAX_TEXT)
            validation = shell.validation_key(raw)
            reads = shell.bash_reads(raw, cwd)
            if reads is None:
                self.unknown['bash command that does not tokenize'] += 1
            for read in reads or []:
                self.conn.execute(
                    'INSERT OR REPLACE INTO reads (tool_use_id, path, start_line, end_line, full, via) '
                    "VALUES (?, ?, ?, ?, ?, 'bash')",
                    (block['id'], read.path, read.start_line, read.end_line, int(read.full)))
        elif kind == 'skill':
            command = str(tool_input.get('skill') or '')
        elif kind == 'agent':
            command = _clip(tool_input.get('description'), MAX_COMMAND_LABEL)
        self.conn.execute(
            'INSERT INTO tool_calls (tool_use_id, session_id, agent_id, message_id, ts_call, tool, '
            'kind, path, command, validation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT (tool_use_id) DO UPDATE SET session_id = excluded.session_id, '
            'agent_id = excluded.agent_id, message_id = excluded.message_id, '
            'ts_call = excluded.ts_call, tool = excluded.tool, kind = excluded.kind, '
            'path = excluded.path, command = excluded.command, validation = excluded.validation',
            (block['id'], session_id, agent_id, message_id, ts, tool, kind,
             str(path) if path else None, command, validation))

    def _user(self, obj: dict, ts: float, session_id: str, agent_id: str) -> None:
        message = obj.get('message') if isinstance(obj.get('message'), dict) else {}
        content = message.get('content')
        results = [block for block in content if isinstance(block, dict)
                   and block.get('type') == 'tool_result'] if isinstance(content, list) else []
        if results:
            for block in results:
                self._tool_result(obj, block, ts, session_id, agent_id)
            if len(results) == 1 and isinstance(obj.get('toolUseResult'), dict):
                self._file_effects(str(results[0].get('tool_use_id') or ''), obj['toolUseResult'])
            return
        if obj.get('isMeta') or obj.get('isCompactSummary'):
            return
        text = _block_text(content)
        if not text:
            return
        origin = obj.get('origin') if isinstance(obj.get('origin'), dict) else {}
        if text.lstrip().startswith(('<command-name>', '<local-command')):
            source = 'local-command'  # /clear, /model and their output: not a request
        else:
            source = str(obj.get('promptSource') or origin.get('kind')
                         or ('agent-brief' if agent_id else 'unlabelled'))
        self.conn.execute(
            'INSERT OR IGNORE INTO prompts (uuid, session_id, agent_id, ts, source, chars, text) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (obj.get('uuid'), session_id, agent_id, ts, source, len(text), _clip(text, MAX_TEXT)))

    def _tool_result(self, obj: dict, block: dict, ts: float, session_id: str, agent_id: str) -> None:
        text = _block_text(block.get('content'))
        is_error = bool(block.get('is_error'))
        exit_match = _EXIT_CODE_RE.match(text)
        decision = obj.get('permissionDecision') if isinstance(obj.get('permissionDecision'), dict) else {}
        permission = (f"{decision.get('source')}:{decision.get('decision')}"
                      if decision.get('source') else None)
        shaped = obj.get('toolUseResult')
        is_bash = isinstance(shaped, dict) and 'stdout' in shaped
        tail = playbook_core.scrub_secrets(text)[-MAX_TAIL:] if is_bash else None
        self.conn.execute(
            'INSERT INTO tool_calls (tool_use_id, session_id, agent_id, ts_result, is_error, '
            'exit_code, error_head, denial, permission, result_chars, result_tail) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT (tool_use_id) DO UPDATE SET ts_result = excluded.ts_result, '
            'is_error = excluded.is_error, exit_code = excluded.exit_code, '
            'error_head = excluded.error_head, denial = excluded.denial, '
            'permission = excluded.permission, result_chars = excluded.result_chars, '
            'result_tail = excluded.result_tail',
            (block.get('tool_use_id'), session_id, agent_id, ts, int(is_error),
             int(exit_match.group(1)) if exit_match else None,
             _clip(text, MAX_HEAD) if is_error else None,
             obj.get('toolDenialKind'), permission, len(text), tail))

    def _file_effects(self, tool_use_id: str, result: dict) -> None:
        """Reads and edits a tool result reports, with their line ranges."""
        viewed = result.get('file')
        if result.get('type') == 'text' and isinstance(viewed, dict) and viewed.get('filePath'):
            start = int(viewed.get('startLine') or 1)
            lines, total = viewed.get('numLines'), viewed.get('totalLines')
            known = isinstance(lines, int) and isinstance(total, int)
            self.conn.execute(
                'INSERT OR REPLACE INTO reads (tool_use_id, path, start_line, end_line, full, via) '
                "VALUES (?, ?, ?, ?, ?, 'read')",
                (tool_use_id, viewed['filePath'], start,
                 start + lines - 1 if isinstance(lines, int) else None,
                 int(known and lines >= total)))
        if result.get('filePath') and isinstance(result.get('structuredPatch'), list):
            hunks = result['structuredPatch']
            if not hunks and isinstance(result.get('content'), str):
                # A file created whole: one hunk covering all of it.
                hunks = [{'oldStart': 0, 'oldLines': 0, 'newStart': 1,
                          'newLines': result['content'].count('\n') + 1}]
            self._hunks(tool_use_id, str(result['filePath']), hunks, 'edit')
        diff = result.get('bashEditDiff')
        if isinstance(diff, dict):
            for changed in diff.get('files') or []:
                if isinstance(changed, dict) and changed.get('filePath'):
                    self._hunks(tool_use_id, str(changed['filePath']), changed.get('hunks') or [], 'bash')

    def _hunks(self, tool_use_id: str, path: str, hunks: list, via: str) -> None:
        for index, hunk in enumerate(hunks):
            if not isinstance(hunk, dict):
                continue
            self.conn.execute(
                'INSERT OR REPLACE INTO edits (tool_use_id, path, hunk, old_start, old_lines, '
                'new_start, new_lines, via) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (tool_use_id, path, index, int(hunk.get('oldStart') or 0), int(hunk.get('oldLines') or 0),
                 int(hunk.get('newStart') or 0), int(hunk.get('newLines') or 0), via))

    def _attachment(self, obj: dict, ts: float, session_id: str, agent_id: str) -> None:
        attachment = obj.get('attachment') if isinstance(obj.get('attachment'), dict) else {}
        kind = str(attachment.get('type') or '')
        if not kind.startswith('hook_'):
            return  # context the harness attached; nothing a hook decided
        content = attachment.get('content')
        blocking = attachment.get('blockingError')
        text = ('\n'.join(str(part) for part in content) if isinstance(content, list)
                else str(content or ''))
        if isinstance(blocking, dict):
            text = str(blocking.get('blockingError') or '')
        text = text or str(attachment.get('stdout') or '') or str(attachment.get('stderr') or '')
        gate = _GATE_RE.search(text)
        self.conn.execute(
            'INSERT OR IGNORE INTO hook_events (uuid, session_id, agent_id, ts, event, hook_name, '
            'tool_use_id, outcome, command, duration_ms, exit_code, gate, text) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (obj.get('uuid'), session_id, agent_id, ts, attachment.get('hookEvent'),
             attachment.get('hookName'), attachment.get('toolUseID'), kind[len('hook_'):],
             _clip(attachment.get('command'), MAX_COMMAND_LABEL) or None,
             attachment.get('durationMs'), attachment.get('exitCode'),
             gate.group(1) if gate else None, _clip(text, MAX_HEAD)))


def ingest(conn: sqlite3.Connection, roots: list[str] | None = None,
           days: float | None = None) -> Ingester:
    ingester = Ingester(conn)
    for path, account, project in transcripts(roots, days):
        try:
            ingester.ingest_file(path, account, project)
        except OSError:
            ingester.unknown['<unreadable file>'] += 1
    ingester.finish()
    return ingester
