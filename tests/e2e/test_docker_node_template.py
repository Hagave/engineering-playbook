"""
Real build and run of the engineering-docker templates for Node.js.

Opt-in: set PLAYBOOK_TEMPLATE_IT=1. It needs Docker with Compose, npm and
network access, and takes a few minutes. It is skipped otherwise.

The Dockerfile is every dockerfile block of references/node.md in order, the
compose file is the services block of the skill's core, and the .dockerignore
joins the core list and the Node additions: the test proves the text the model
reads. Only a minimal app is written here: one Prisma model, one migration and
an HTTP server that counts rows through the generated client. Packages are
installed at their latest version, as the skills' install commands do.

Checked: the migrate service runs the migration and exits 0, and the app starts
only after it finished; the production command runs no migration; the app
answers through the pruned image, as a non-root user; and `docker compose stop`
ends it through SIGTERM, which dumb-init forwards.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILL_DIR = os.path.join(_ROOT, 'skills', 'engineering-docker')
_UP_TIMEOUT_S = 900
_HEALTH_TIMEOUT_S = 60.0

# Latest, except where a skill this test exercises is written for an older
# major: engineering-prisma is written for Prisma 7, and the `latest` tag of the
# prisma CLI is 8, which has no `prisma generate`. The pin is a known gap of
# that skill, not of this one; it goes when engineering-prisma follows Prisma 8.
_PINNED = {'prisma': '^7', '@prisma/client': '^7', '@prisma/adapter-pg': '^7'}

_PACKAGE = {
    'name': 'docker-node-template-it',
    'private': True,
    'scripts': {'build': 'tsc -p tsconfig.json'},
    'dependencies': {name: _PINNED.get(name, 'latest') for name in ('@prisma/adapter-pg', '@prisma/client')},
    'devDependencies': {name: _PINNED.get(name, 'latest') for name in ('@types/node', 'prisma', 'typescript')},
}

_TSCONFIG = {
    'compilerOptions': {
        'module': 'commonjs', 'target': 'ES2022', 'outDir': 'dist', 'rootDir': 'src',
        'strict': True, 'esModuleInterop': True, 'skipLibCheck': True,
    },
    'include': ['src'],
}

_PRISMA_CONFIG = """\
import { defineConfig } from 'prisma/config';

export default defineConfig({
  schema: 'src/infrastructure/database/prisma',
  migrations: { path: 'src/infrastructure/database/prisma/migrations' },
  datasource: { url: process.env['DATABASE_URL'] ?? 'postgresql://build@localhost/build' },
});
"""

_SCHEMA = """\
generator client {
  provider     = "prisma-client"
  output       = "./generated/prisma"
  moduleFormat = "cjs"
}

datasource db {
  provider = "postgresql"
}
"""

_MODEL = """\
model Item {
  id   Int    @id @default(autoincrement())
  name String
}
"""

_MIGRATION = """\
CREATE TABLE "Item" (
    "id" SERIAL NOT NULL,
    "name" TEXT NOT NULL,
    CONSTRAINT "Item_pkey" PRIMARY KEY ("id")
);
"""

_MAIN = """\
import { createServer } from 'node:http';
import { PrismaPg } from '@prisma/adapter-pg';
import { PrismaClient } from './infrastructure/database/prisma/generated/prisma/client';

const prisma = new PrismaClient({
  adapter: new PrismaPg({ connectionString: process.env['DATABASE_URL'] ?? '' }),
});

const server = createServer((request, response): void => {
  if (request.url !== '/health') {
    response.writeHead(404).end();
    return;
  }
  prisma.item
    .count()
    .then((items: number): void => {
      response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ items }));
    })
    .catch((error: unknown): void => {
      response.writeHead(500).end(String(error));
    });
});

server.listen(Number(process.env['PORT'] ?? 8080), '0.0.0.0');
"""
# The server installs no SIGTERM handler on purpose: as PID 1, Node ignores a
# signal it has no handler for, so only a forwarding init lets `stop` end it
# with SIGTERM (exit 143) instead of the SIGKILL after the timeout (exit 137).

_OVERRIDE = """\
services:
  app:
    ports:
      - '127.0.0.1::8080'
"""


def _blocks(text: str, language: str) -> list[str]:
    """The fenced blocks tagged `language` ('' for an untagged fence), read
    fence by fence: a regex would take a closing fence for an untagged opener."""
    found: list[str] = []
    current: list[str] | None = None
    tag = ''
    for line in text.splitlines(keepends=True):
        fence = re.match(r'^```(\S*)\s*$', line)
        if fence and current is None:
            current, tag = [], fence.group(1)
        elif fence:
            if tag == language:
                found.append(''.join(current))
            current = None
        elif current is not None:
            current.append(line)
    return found


def _read(rel: str) -> str:
    with open(os.path.join(_SKILL_DIR, rel), encoding='utf-8') as f:
        return f.read()


@unittest.skipUnless(os.environ.get('PLAYBOOK_TEMPLATE_IT') == '1',
                     'opt-in: PLAYBOOK_TEMPLATE_IT=1 (needs Docker with Compose, npm and network)')
class TestDockerNodeTemplate(unittest.TestCase):

    app_dir = ''
    project = ''

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_dir = tempfile.mkdtemp(prefix='docker_node_it_')
        cls.project = f'pbdockerit{uuid.uuid4().hex[:8]}'
        node = _read('references/node.md')
        core = _read('SKILL.md')
        compose = [b for b in _blocks(core, 'yaml') if 'x-database' in b]
        if len(compose) != 1:
            raise AssertionError(f'expected one compose block with x-database in the skill, found {len(compose)}')
        prisma_dir = 'src/infrastructure/database/prisma'
        files = {
            'Dockerfile': '\n'.join(_blocks(node, 'dockerfile')),
            'compose.yaml': compose[0],
            'compose.override.yaml': _OVERRIDE,
            '.dockerignore': '\n'.join(_blocks(core, '') + _blocks(node, '')),
            'docker/entrypoint-development.sh': _blocks(node, 'bash')[0],
            'package.json': json.dumps(_PACKAGE, indent=2),
            'tsconfig.json': json.dumps(_TSCONFIG, indent=2),
            'prisma.config.ts': _PRISMA_CONFIG,
            f'{prisma_dir}/schema.prisma': _SCHEMA,
            f'{prisma_dir}/models/item.prisma': _MODEL,
            f'{prisma_dir}/migrations/20260101000000_init/migration.sql': _MIGRATION,
            f'{prisma_dir}/migrations/migration_lock.toml': 'provider = "postgresql"\n',
            'src/main.ts': _MAIN,
        }
        for rel, content in files.items():
            path = os.path.join(cls.app_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        cls._run(['npm', 'install', '--package-lock-only', '--ignore-scripts', '--no-audit', '--no-fund'],
                 timeout=300)
        with open(os.path.join(cls.app_dir, 'package-lock.json'), encoding='utf-8') as f:
            locked = json.load(f)
        resolved = {name[len('node_modules/'):]: meta.get('version') for name, meta in locked['packages'].items()
                    if name.count('node_modules/') == 1 and name[len('node_modules/'):] in
                    {*_PACKAGE['dependencies'], *_PACKAGE['devDependencies']}}
        sys.stderr.write(f'\nResolved versions: {json.dumps(resolved, sort_keys=True)}\n')
        try:
            cls._run(cls._compose('up', '-d', '--build'), timeout=_UP_TIMEOUT_S)
        except BaseException:
            cls.tearDownClass()  # unittest skips tearDownClass when setUpClass raises
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.project:
            subprocess.run(cls._compose('down', '-v', '--rmi', 'local', '--remove-orphans'),
                           capture_output=True, text=True, timeout=300, check=False)
        shutil.rmtree(cls.app_dir, ignore_errors=True)

    @classmethod
    def _compose(cls, *args: str) -> list[str]:
        return ['docker', 'compose', '-p', cls.project, '--project-directory', cls.app_dir,
                '-f', os.path.join(cls.app_dir, 'compose.yaml'),
                '-f', os.path.join(cls.app_dir, 'compose.override.yaml'), *args]

    @classmethod
    def _run(cls, command: list[str], timeout: int = 120) -> str:
        done = subprocess.run(command, cwd=cls.app_dir, capture_output=True, text=True,
                              timeout=timeout, check=False)
        if done.returncode != 0:
            logs = subprocess.run(cls._compose('logs', '--no-color', 'migrate', 'app'), capture_output=True, text=True,
                                  timeout=60, check=False).stdout if cls.project else ''
            raise AssertionError(f'{" ".join(command)} failed ({done.returncode}):\n'
                                 f'{done.stdout[-4000:]}\n{done.stderr[-4000:]}\n{logs[-4000:]}')
        return done.stdout

    def _state(self, service: str) -> dict:
        out = self._run(self._compose('ps', '-a', '--format', 'json', service))
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(rows), 1, out)
        return rows[0]

    def _health(self) -> tuple[int, str]:
        port = self._run(self._compose('port', 'app', '8080')).strip().rsplit(':', 1)[-1]
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last = ''
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as response:
                    return response.status, response.read().decode()
            except Exception as error:  # noqa: BLE001 -- retried until the deadline, then reported
                last = repr(error)
                time.sleep(1)
        self.fail(f'/health never answered: {last}\n{self._run(self._compose("logs", "--no-color", "app"))}')

    def test_1_the_migration_runs_as_its_own_step_and_exits_0(self) -> None:
        state = self._state('migrate')
        self.assertEqual((state.get('State'), state.get('ExitCode')), ('exited', 0), state)

    def test_2_the_app_answers_through_the_pruned_production_image(self) -> None:
        status, body = self._health()
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {'items': 0})

    def _inspect(self, service: str) -> dict:
        container = self._run(self._compose('ps', '-a', '-q', service)).strip()
        return json.loads(self._run(['docker', 'inspect', container]))[0]

    def test_3_the_app_starts_only_after_the_migration_finished(self) -> None:
        migrated = self._inspect('migrate')['State']['FinishedAt']
        started = self._inspect('app')['State']['StartedAt']
        self.assertLessEqual(migrated[:26], started[:26], f'migrate finished {migrated}, app started {started}')

    def test_3b_the_production_command_runs_no_migration(self) -> None:
        config = self._inspect('app')['Config']
        command = ' '.join((config.get('Entrypoint') or []) + (config.get('Cmd') or []))
        self.assertNotIn('migrate', command)
        self.assertEqual(config.get('Cmd'), ['node', 'dist/main.js'])

    def test_4_the_app_runs_as_a_non_root_user(self) -> None:
        self.assertEqual(self._run(self._compose('exec', '-T', 'app', 'id', '-u')).strip(), '1001')

    def test_5_stop_reaches_the_app_through_sigterm(self) -> None:
        self._health()
        started = time.monotonic()
        self._run(self._compose('stop', '-t', '20', 'app'), timeout=60)
        elapsed = time.monotonic() - started
        state = self._state('app')
        self.assertEqual(state.get('ExitCode'), 143, f'the app did not end on SIGTERM: {state}')
        self.assertLess(elapsed, 15, 'stop waited for the kill timeout: SIGTERM did not reach the app')


if __name__ == '__main__':
    unittest.main()
