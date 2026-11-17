"""
End-to-end run of the local validation lab against a throwaway compose
project, with real Docker.

Opt-in: set PLAYBOOK_LAB_IT=1. It needs Docker, the scanner image
(`python3 lab/run.py --setup` downloads it: about 1.2 GB) and the small
`python:3.12-alpine` image, and it takes minutes. It is skipped otherwise,
so tests/run_all.py stays fast and offline.
"""
import base64
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


# A deliberately SQL-injectable app, for the sqlmap mode only. It interpolates
# the `id` query parameter straight into a SQLite query, so `id=1 AND 1=1` vs
# `1 AND 1=2` return different bodies (boolean-based) and `id=1'` returns a DBMS
# error (error-based) -- enough for sqlmap at level/risk 1 to confirm CWE-89.
# It runs entirely in the image's own Python (no bind mount, which the lab
# forbids): the script is base64'd into a `python3 -c` so nothing is copied in.
_VULN_APP = """\
import http.server, sqlite3, urllib.parse
db = sqlite3.connect(":memory:", check_same_thread=False)
db.execute("CREATE TABLE items(id INTEGER, name TEXT)")
db.executemany("INSERT INTO items VALUES (?, ?)", [(1, "alpha"), (2, "beta")])

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        value = q.get("id", ["1"])[0]
        try:
            cur = db.execute("SELECT name FROM items WHERE id = %s" % value)
            body = " ".join(r[0] for r in cur.fetchall()) or "none"
        except Exception as exc:
            body = "SQLITE_ERROR: %s" % exc
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass

http.server.HTTPServer(("0.0.0.0", 8000), H).serve_forever()
"""
_VULN_B64 = base64.b64encode(_VULN_APP.encode()).decode()
VULN_COMPOSE = (
    "services:\n"
    "  app:\n"
    "    image: python:3.12-alpine\n"
    '    command: ["python3", "-c", '
    f"\"import base64; exec(base64.b64decode('{_VULN_B64}'))\"]\n"
)


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
            capture_output=True, text=True, timeout=1200)

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

    def test_active_scan_runs_the_full_scan_and_leaves_nothing_behind(self):
        result = self._run('--active', '--active-minutes', '2')
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertIn('OWASP ZAP full scan, active', result.stdout)
        self.assertEqual(self._leftovers(), [])

    def test_a_confirmed_injection_is_reported(self):
        with open(os.path.join(self.project, 'compose.yaml'), 'w') as fh:
            fh.write(VULN_COMPOSE)
        result = self._run('--sqlmap', '--endpoint', '/items?id=1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('SQL injection in GET parameter', result.stdout)
        self.assertIn('CWE-89', result.stdout)
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
