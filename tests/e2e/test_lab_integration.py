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

    def test_a_mount_outside_the_project_is_refused_before_anything_starts(self):
        with open(os.path.join(self.project, 'compose.yaml'), 'a') as fh:
            fh.write('    volumes: ["/etc/hostname:/srv/h:ro"]\n')
        result = self._run()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('outside the project', result.stderr)
        self.assertEqual(self._leftovers(), [])

    def test_a_writable_bind_inside_the_project_is_dropped_and_the_lab_runs(self):
        # The drop depends on `docker compose config` normalizing this short
        # form to long form; a unit test cannot prove that, so it is run against
        # the real tool here. The rw bind is dropped and the scan completes.
        with open(os.path.join(self.project, 'compose.yaml'), 'a') as fh:
            fh.write('    volumes: ["./:/srv"]\n')
        result = self._run()
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        self.assertNotIn('outside the project', result.stderr)
        self.assertEqual(self._leftovers(), [])


@unittest.skipUnless(os.environ.get('PLAYBOOK_LAB_IT') == '1',
                     'opt-in: PLAYBOOK_LAB_IT=1 (needs Docker, git and the scanner image)')
class TestGateRun(unittest.TestCase):
    """--gate builds and scans the committed tree, not the working tree, and
    records the result against the commit. A unit test mocks git and Docker, so
    the real behaviour — building from a worktree of the commit — is proven here:
    the working tree is left with a Dockerfile that cannot build, so a scan of it
    would fail; a clean run proves the committed tree was used."""

    DOCKERFILE = (
        'FROM python:3.12-alpine\n'
        'WORKDIR /srv\n'
        'RUN printf \'<html><body><a href="/a.html">a</a></body></html>\' > index.html '
        '&& printf \'<html>a</html>\' > a.html\n'
        'CMD ["python", "-m", "http.server", "8000"]\n'
    )
    GATE_COMPOSE = 'services:\n  app:\n    build: ./app\n    ports: ["18000:8000"]\n'

    def setUp(self):
        self.project = os.path.realpath(tempfile.mkdtemp(prefix='labgate'))
        self.addCleanup(shutil.rmtree, self.project, True)
        self.state = tempfile.mkdtemp(prefix='labstate')
        self.addCleanup(shutil.rmtree, self.state, True)
        os.makedirs(os.path.join(self.project, 'app'))
        self._write('compose.yaml', self.GATE_COMPOSE)
        self._write('app/Dockerfile', self.DOCKERFILE)
        self._git('init', '-q')
        self._git('config', 'user.email', 'lab@example.com')
        self._git('config', 'user.name', 'lab')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'committed')
        self.head = self._git('rev-parse', 'HEAD').strip()
        self._write('app/Dockerfile', 'FROM redlab-nonexistent-base:zzz\n')  # dirty, unbuildable
        self.lab_project = 'redlab-' + os.path.basename(self.project).lower()

    def _write(self, rel: str, text: str) -> None:
        with open(os.path.join(self.project, rel), 'w') as fh:
            fh.write(text)

    def _git(self, *args: str) -> str:
        return subprocess.run(('git', '-C', self.project) + args,
                              capture_output=True, text=True, check=True).stdout

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        env = {**os.environ, 'REDLAB_STATE_DIR': self.state}
        return subprocess.run(
            [sys.executable, _RUN, '--project-dir', self.project,
             '--minutes', '1', '--startup-seconds', '3', *extra],
            capture_output=True, text=True, timeout=1200, env=env)

    def test_gate_builds_the_commit_records_the_result_and_cleans_up(self):
        import json
        result = self._run('--gate')
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        files = os.listdir(self.state)
        self.assertEqual(len(files), 1, files)
        with open(os.path.join(self.state, files[0])) as fh:
            record = json.load(fh)
        self.assertEqual(record['commit'], self.head)
        self.assertIn(record['verdict'], (0, 1))
        self.assertEqual([t['service'] for t in record['targets']], ['app'])
        # No git worktree and no container, network or volume left behind.
        worktrees = [line for line in self._git('worktree', 'list').splitlines() if line.strip()]
        self.assertEqual(len(worktrees), 1, worktrees)
        label = f'label=com.docker.compose.project={self.lab_project}'
        self.assertEqual(_docker('ps', '-aq', '--filter', label), '')


# An API with two banks and three customers, for the identity mode. It serves
# its OpenAPI document under the global prefix /api/v1, a JSON login, and a
# list of the signed-in customer's accounts.
_API_APP = """\
import http.server, json
USERS = {"11111111111": ("alice", "pw-alice"), "22222222222": ("bob", "pw-bob"),
         "33333333333": ("carol", "pw-carol")}
DOC = {"openapi": "3.0.0", "paths": {
    "/api/v1/auth/login": {"post": {}}, "/api/v1/accounts": {"get": {}},
    "/api/v1/accounts/{id}": {"get": {}}, "/api/v1/accounts/{id}/statement": {"get": {}}}}

class H(http.server.BaseHTTPRequestHandler):
    def send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if self.path == "/api-json":
            self.send(200, DOC)
        elif self.path == "/api/v1/accounts" and token.startswith("t-"):
            self.send(200, {"data": [{"id": token[2:] + "-acc"}]})
        elif self.path.startswith("/api/v1/"):
            self.send(401, {"error": "unauthorized"})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        user = USERS.get(body.get("cpf"))
        if self.path == "/api/v1/auth/login" and user and body.get("password") == user[1]:
            self.send(200, {"accessToken": "t-" + user[0]})
        else:
            self.send(401, {"error": "invalid credentials"})

    def log_message(self, *a):
        pass

http.server.HTTPServer(("0.0.0.0", 8000), H).serve_forever()
"""
_API_B64 = base64.b64encode(_API_APP.encode()).decode()
API_COMPOSE = (
    "services:\n"
    "  app:\n"
    "    build: ./app\n"
    "    ports: [\"18000:8000\"]\n"
)
API_DOCKERFILE = (
    'FROM python:3.12-alpine\n'
    f'CMD ["python3", "-c", "import base64; exec(base64.b64decode(\'{_API_B64}\'))"]\n'
)
# What the identity mode reads the routes from: the declarations in the code.
API_CONTROLLER = (
    "@Controller('accounts')\nexport class AccountsController {\n"
    "  @Get() list() {}\n  @Get(':id') one() {}\n  @Get(':id/statement') statement() {}\n}\n"
    "@Controller('auth')\nexport class AuthController { @Post('login') login() {} }\n"
)


@unittest.skipUnless(os.environ.get('PLAYBOOK_LAB_IT') == '1',
                     'opt-in: PLAYBOOK_LAB_IT=1 (needs Docker and the client image)')
class TestIdentityRun(unittest.TestCase):
    """--identities against a real container: the client signs each customer in
    from inside the lab, lists what each owns, reads the prefix from the OpenAPI
    document, and nothing it prints carries a password or a token."""

    def setUp(self):
        import json
        self.project = os.path.realpath(tempfile.mkdtemp(prefix='labids'))
        self.addCleanup(shutil.rmtree, self.project, True)
        os.makedirs(os.path.join(self.project, 'app'))
        os.makedirs(os.path.join(self.project, 'src'))
        for rel, text in (('compose.yaml', API_COMPOSE), ('app/Dockerfile', API_DOCKERFILE),
                          ('src/accounts.controller.ts', API_CONTROLLER)):
            with open(os.path.join(self.project, rel), 'w') as fh:
                fh.write(text)
        self.outside = tempfile.mkdtemp(prefix='labpersonas')
        self.addCleanup(shutil.rmtree, self.outside, True)

        def persona(name: str, cpf: str, tenant: str) -> dict:
            return {'name': name, 'role': 'customer', 'tenant': tenant,
                    'login': {'method': 'POST', 'path': '/api/v1/auth/login',
                              'body': {'cpf': cpf, 'password': f'pw-{name}'}},
                    'token': {'json': '/accessToken'},
                    'send': {'header': 'Authorization', 'scheme': 'Bearer'}}

        self.personas = os.path.join(self.outside, 'personas.json')
        with open(self.personas, 'w') as fh:
            json.dump({'personas': [persona('alice', '11111111111', '001'),
                                    persona('bob', '22222222222', '001'),
                                    persona('carol', '33333333333', '002')]}, fh)
        self.lab_project = 'redlab-' + os.path.basename(self.project).lower()

    def test_signs_in_lists_and_reports_without_a_credential(self):
        result = subprocess.run(
            [sys.executable, _RUN, '--project-dir', self.project, '--identities', self.personas,
             '--startup-seconds', '2'],
            capture_output=True, text=True, timeout=1200, check=False)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(output.count(': signed in (login status 200)'), 3, output)
        self.assertIn('(served under /api/v1)', output)
        self.assertIn('OpenAPI document: /api-json (4 operations)', output)
        self.assertIn('aimable at a resource whose owner is known: 2', output)
        for secret in ('pw-alice', 'pw-bob', 'pw-carol', 't-alice', 't-bob', 't-carol'):
            self.assertNotIn(secret, output)
        label = f'label=com.docker.compose.project={self.lab_project}'
        self.assertEqual(_docker('ps', '-aq', '--filter', label), '')


if __name__ == '__main__':
    unittest.main()
