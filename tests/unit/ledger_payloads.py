"""
Hook payloads for the ledger tests, in the shapes Claude Code 2.1.327 sent to
a probe plugin that logged every hook input (an interactive session for the
/clear chain, the background task and the notification turn; a headless one
for Read, Edit, Write, NotebookEdit, a subagent and a slash command). Field
sets and nesting are the observed ones; ids, paths and texts are replaced.
"""

TRANSCRIPTS = '/home/user/.claude/projects/-work-project'


def common(session_id: str, cwd: str, prompt_id: str | None = None, mode: str | None = 'auto') -> dict:
    payload = {
        'session_id': session_id,
        'transcript_path': f'{TRANSCRIPTS}/{session_id}.jsonl',
        'cwd': cwd,
        'scratchpad_dir': f'/tmp/claude-1000/-work-project/{session_id}/scratchpad',
    }
    if prompt_id is not None:
        payload['prompt_id'] = prompt_id
    if mode is not None:
        payload['permission_mode'] = mode
    return payload


def session_start(session_id: str, cwd: str, source: str, model: str | None = None) -> dict:
    payload = {**common(session_id, cwd, mode=None), 'hook_event_name': 'SessionStart', 'source': source}
    if model:
        payload['model'] = model
    return payload


def session_end(session_id: str, cwd: str, prompt_id: str, reason: str) -> dict:
    return {**common(session_id, cwd, prompt_id, mode=None), 'hook_event_name': 'SessionEnd', 'reason': reason}


def prompt(session_id: str, cwd: str, prompt_id: str, text: str) -> dict:
    return {**common(session_id, cwd, prompt_id), 'hook_event_name': 'UserPromptSubmit', 'prompt': text}


def expansion(session_id: str, cwd: str, prompt_id: str, command: str, args: str = '') -> dict:
    return {**common(session_id, cwd, prompt_id, mode='default'), 'hook_event_name': 'UserPromptExpansion',
            'expansion_type': 'slash_command', 'command_name': command, 'command_args': args,
            'command_source': 'projectSettings', 'prompt': f'/{command}' + (f' {args}' if args else '')}


def notification(task_id: str, tool_use_id: str) -> str:
    return (f'<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n'
            f'<output-file>/tmp/claude-1000/-work-project/s/tasks/{task_id}.output</output-file>\n'
            '<status>completed</status>\n<summary>Background command "Wait then print done" completed '
            '(exit code 0)</summary>\n</task-notification>')


def _tool(session_id: str, cwd: str, prompt_id: str, event: str, tool: str, tool_input: dict,
          tool_use_id: str, agent: tuple[str, str] | None = None) -> dict:
    payload = {**common(session_id, cwd, prompt_id), 'effort': {'level': 'high'}}
    if agent:
        payload['agent_id'], payload['agent_type'] = agent
    payload.update({'hook_event_name': event, 'tool_name': tool, 'tool_input': tool_input,
                    'tool_use_id': tool_use_id})
    return payload


def read(session_id: str, cwd: str, prompt_id: str, tool_use_id: str, path: str, content: str,
         agent: tuple[str, str] | None = None) -> dict:
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUse', 'Read', {'file_path': path}, tool_use_id, agent)
    lines = content.count('\n') + 1
    payload['tool_response'] = {'type': 'text', 'file': {
        'filePath': path, 'content': content, 'numLines': lines, 'startLine': 1, 'totalLines': lines}}
    payload['duration_ms'] = 8
    return payload


def read_notebook(session_id: str, cwd: str, prompt_id: str, tool_use_id: str, path: str) -> dict:
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUse', 'Read', {'file_path': path}, tool_use_id)
    payload['tool_response'] = {'type': 'notebook', 'file': {'filePath': path, 'cells': [
        {'cellType': 'code', 'source': 'print(1)', 'cell_id': 'cell-0', 'language': 'python'}]}}
    payload['duration_ms'] = 51
    return payload


def edit(session_id: str, cwd: str, prompt_id: str, tool_use_id: str, path: str, old: str, new: str,
         original: str) -> dict:
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUse', 'Edit',
                    {'file_path': path, 'old_string': old, 'new_string': new, 'replace_all': False}, tool_use_id)
    payload['tool_response'] = {
        'filePath': path, 'oldString': old, 'newString': new, 'originalFile': original,
        'structuredPatch': [{'oldStart': 1, 'oldLines': 2, 'newStart': 1, 'newLines': 2,
                             'lines': [f' {original.splitlines()[0]}', f'-{old}', f'+{new}']}],
        'userModified': False, 'replaceAll': False}
    payload['duration_ms'] = 12
    return payload


def write(session_id: str, cwd: str, prompt_id: str, tool_use_id: str, path: str, content: str) -> dict:
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUse', 'Write',
                    {'file_path': path, 'content': content}, tool_use_id)
    payload['tool_response'] = {'type': 'create', 'filePath': path, 'content': content, 'structuredPatch': [],
                                'originalFile': None, 'userModified': False}
    payload['duration_ms'] = 5
    return payload


def notebook_edit(session_id: str, cwd: str, prompt_id: str, tool_use_id: str, path: str) -> dict:
    tool_input = {'notebook_path': path, 'cell_id': 'cell-0', 'new_source': 'print(2)', 'edit_mode': 'replace'}
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUse', 'NotebookEdit', tool_input, tool_use_id)
    payload['tool_response'] = {'new_source': 'print(2)', 'old_source': 'print(1)', 'cell_type': 'code',
                                'language': 'python', 'edit_mode': 'replace', 'cell_id': 'cell-0', 'error': '',
                                'notebook_path': path, 'original_file': '{"cells": []}', 'updated_file': '{}'}
    payload['duration_ms'] = 20
    return payload


def bash(session_id: str, cwd: str, prompt_id: str, tool_use_id: str, command: str,
         stdout: str = '', background_task_id: str | None = None,
         agent: tuple[str, str] | None = None) -> dict:
    tool_input = {'command': command, 'description': 'Run it'}
    if background_task_id:
        tool_input['run_in_background'] = True
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUse', 'Bash', tool_input, tool_use_id, agent)
    payload['tool_response'] = {'stdout': stdout, 'stderr': '', 'interrupted': False, 'isImage': False,
                                'noOutputExpected': False}
    if background_task_id:
        payload['tool_response']['backgroundTaskId'] = background_task_id
    payload['duration_ms'] = 386
    return payload


def bash_failure(session_id: str, cwd: str, prompt_id: str, tool_use_id: str, command: str,
                 error: str = 'Exit code 1') -> dict:
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUseFailure', 'Bash',
                    {'command': command, 'description': 'Run it'}, tool_use_id)
    payload.update({'error': error, 'is_interrupt': False, 'duration_ms': 30})
    return payload


def ask(session_id: str, cwd: str, prompt_id: str, tool_use_id: str) -> dict:
    """Not observed: AskUserQuestion is unavailable headless. The input is the
    documented tool input; the response is only something to keep whole."""
    tool_input = {'questions': [{'question': 'Which store?', 'header': 'Store', 'multiSelect': False,
                                 'options': [{'label': 'SQLite', 'description': 'one file'},
                                             {'label': 'JSON', 'description': 'many files'}]}]}
    payload = _tool(session_id, cwd, prompt_id, 'PostToolUse', 'AskUserQuestion', tool_input, tool_use_id)
    payload['tool_response'] = {'questions': tool_input['questions'], 'answers': {'Which store?': 'SQLite'}}
    return payload


def stop(session_id: str, cwd: str, prompt_id: str, message: str, background: list[dict] | None = None,
         active: bool = False) -> dict:
    return {**common(session_id, cwd, prompt_id), 'effort': {'level': 'high'}, 'hook_event_name': 'Stop',
            'stop_hook_active': active, 'last_assistant_message': message,
            'background_tasks': background or [], 'session_crons': []}


def background_task(task_id: str, command: str) -> dict:
    return {'id': task_id, 'type': 'shell', 'status': 'running', 'description': 'Wait then print done',
            'command': command}
