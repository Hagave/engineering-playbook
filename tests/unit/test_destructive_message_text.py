"""
A message that talks about a destructive command is text, not the command: a
commit that explains a fix to the destructive-command gate asked the user about
the `git reset --hard` in its own message. The words after the message, and
anything the shell would run from inside it, are still read.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core

# (command, whether the gate must still ask)
CORPUS = [
    # Text in a message option: nothing to ask.
    ('git commit -m "explain why git reset --hard asked"', False),
    ('git commit -q -m "git reset --hard in prose"', False),
    ('git commit -qm "git reset --hard in prose"', False),
    ('git commit -am "git clean -fd in prose"', False),
    ('git commit --message "git reset --hard in prose"', False),
    ('git commit --message="git reset --hard in prose"', False),
    ("git commit -m 'single quotes: `git reset --hard` stays text'", False),
    ('git commit -m "escaped: \\`git reset --hard\\` stays text"', False),
    ('git -C /repo commit -n -m "git reset --hard in prose"', False),
    ('git commit -m "first" -m "git reset --hard in the second paragraph"', False),
    ('git stash push -m "git clean -fd note"', False),
    ('git tag -a v1 -m "git reset --hard in prose"', False),
    ('git merge --no-ff -m "git reset --hard in prose" topic', False),
    ('gh pr create --title "git reset --hard" --body "git clean -fd"', False),
    ('gh pr create -t "git reset --hard" -b "git clean -fd"', False),
    ('gh pr comment 5 --body "git reset --hard in prose"', False),
    ('gh issue create --title "x" --body "git reset --hard in prose"', False),
    ('gh release create v1 --notes "git reset --hard in prose"', False),
    # The shell runs these, whatever the message says.
    ('git commit -m "x" && git reset --hard', True),
    ('git commit -m "git reset --hard"; git reset --hard HEAD~1', True),
    ('git commit -m ok; git clean -fd', True),
    ('git commit -m "$(git reset --hard)"', True),
    ('git commit -m "run `git reset --hard` now"', True),
    ('git commit -m $(git reset --hard)', True),
    ('gh pr create --body "x" && git reset --hard', True),
    ('gh pr create --body "$(git clean -fd)"', True),
    ('git tag -d v1 && git reset --hard', True),
    ('git commit -m "x" | git reset --hard', True),
    # Not a command that takes a message: read as before.
    ('echo "git reset --hard"', True),
    ('sh -c "git reset --hard"', True),
    ('bash -c "git commit -m x; git reset --hard"', True),
    ('find . -exec git reset --hard \\;', True),
    ('xargs git reset --hard', True),
    ('watch git reset --hard', True),
    ('flock /tmp/l git reset --hard', True),
    ('eval "git reset --hard"', True),
    ('git rebase --exec "git reset --hard" main', True),
    ('git reset --hard', True),
    ('git commit -m x --amend && git push --force', True),
]


class TestMessageText(unittest.TestCase):

    def test_corpus(self) -> None:
        for command, asks in CORPUS:
            with self.subTest(command=command):
                hit = playbook_core.check_destructive(command, command, '/tmp')
                self.assertEqual(hit is not None, asks, hit)

    def test_the_part_shown_to_the_user_is_the_whole_command_as_typed(self) -> None:
        command = 'git commit -m "x" && git reset --hard'
        hit = playbook_core.check_destructive(command, command, '/tmp')
        self.assertEqual(hit['part'], 'git reset --hard')


if __name__ == '__main__':
    unittest.main()
