"""
End-to-end run of the local validation lab against a throwaway compose
project, with real Docker.

Opt-in: set PLAYBOOK_LAB_IT=1. It needs Docker, the scanner image
(`python3 lab/run.py --setup` downloads it: about 1.2 GB) and the small
`python:3.12-alpine` image, and it takes minutes. It is skipped otherwise,
so tests/run_all.py stays fast and offline.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RUN = os.path.join(_ROOT, 'lab', 'run.py')

# Serves a two-page site from its own directory. Serving the container's root
# instead let the spider walk /proc, and the passive scan never finished.
COMPOSE = """\
services:
  app:
    image: python:3.12-alpine
    command:
      - sh
      - -c
      - >-
        mkdir -p /srv/www && cd /srv/www &&
        echo '<html><body><a href="/about.html">about</a></body></html>' > index.html &&
        echo '<html><body>about</body></html>' > about.html &&
        exec python -m http.server 8000
    ports: ["18000:8000"]
"""


def _docker(*args: str) -> str:
    return subprocess.run(('docker',) + args, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(os.environ.get('PLAYBOOK_LAB_IT') == '1',
                     'opt-in: PLAYBOOK_LAB_IT=1 (needs Docker and the scanner image)')
class TestLabRun(unittest.TestCase):

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix='labit')
        self.addCleanup(shutil.rmtree, self.project, True)
        with open(os.path.join(self.project, 'compose.yaml'), 'w') as fh:
            fh.write(COMPOSE)
        self.lab_project = 'redlab-' + os.path.basename(self.project).lower()

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, _RUN, '--project-dir', self.project, '--service', 'app',
             '--port', '8000', '--minutes', '1', '--startup-seconds', '3', *extra],
            capture_output=True, text=True, timeout=900)

    def _leftovers(self) -> list[str]:
        label = f'label=com.docker.compose.project={self.lab_project}'
        return [x for x in (_docker('ps', '-aq', '--filter', label),
                            _docker('network', 'ls', '-q', '--filter', label),
                            _docker('volume', 'ls', '-q', '--filter', label)) if x]

    def test_scan_reports_and_leaves_nothing_behind(self):
        result = self._run()
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertIn('Lab scan of http://app:8000/', result.stdout)
        self.assertEqual(self._leftovers(), [])

    def test_bind_mount_is_refused_before_anything_starts(self):
        with open(os.path.join(self.project, 'compose.yaml'), 'a') as fh:
            fh.write('    volumes: ["./:/srv"]\n')
        result = self._run()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('--allow-bind-mounts', result.stderr)
        self.assertEqual(self._leftovers(), [])


if __name__ == '__main__':
    unittest.main()
