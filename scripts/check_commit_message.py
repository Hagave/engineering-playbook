#!/usr/bin/env python3
"""
Engineering Playbook — commit message attribution check.

Exits 1 when a commit message file carries AI attribution. The commit-msg git
hook the Antigravity adapter installs calls this script, so the rule lives in
one place (playbook_core.AI_ATTRIBUTION_RE), shared with the Claude Code and
Codex gates, instead of a second copy in shell.

PLAYBOOK_ALLOW_AI_ATTRIBUTION is not honoured here: a git hook inherits the
environment of the `git commit` that runs it, so an agent could set it on its
own command. The user skips the hook once with `git commit --no-verify`.

Usage:
  check_commit_message.py <commit-message-file>
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hooks'))
import playbook_core  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print('usage: check_commit_message.py <commit-message-file>', file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding='utf-8', errors='replace') as f:
            message = f.read()
    except OSError as exc:
        print(f'[playbook] cannot read the commit message: {exc}', file=sys.stderr)
        return 2
    if playbook_core.AI_ATTRIBUTION_RE.search(message):
        print('[playbook] AI attribution detected in the commit message.', file=sys.stderr)
        print('[playbook] Remove the Co-Authored-By / "Generated with" line and retry.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
