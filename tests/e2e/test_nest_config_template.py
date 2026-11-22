"""
Real run of the NestJS environment validation templates.

Opt-in: set PLAYBOOK_TEMPLATE_IT=1. It needs node and npm with network access
(well under a minute to install). It is skipped otherwise.

Every copy of the template is run, each taken from its skill's own code
blocks, not from a copy: engineering-nestjs (Configuration) and
engineering-arch-hexagonal (Env validation). A minimal module around each block
is the only code written here. Both are compiled under the playbook's strict
flags and booted as an application context with no HTTP server.

Checked for each: the app boots with every declared variable set while the
machine's own variables (PATH, HOME) are present, which `allowUnknown: false`
broke; defaults are applied and converted; an undeclared variable is not served
by ConfigService; and a missing or invalid variable stops the boot, with every
missing variable named at once.

Packages are installed at the release a new project installs ("Dependency
versions" in engineering-governance). The scheduled workflow
`.github/workflows/template-latest.yml` runs it weekly.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.e2e.npm_versions import package_json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS = os.path.join(_ROOT, 'skills')
_FENCE_RE = re.compile(r'^```typescript\n(.*?)^```', re.DOTALL | re.MULTILINE)
_RUN_TIMEOUT_S = 60
_DEFAULT_PORT = 3000

_DEPENDENCIES = ('@nestjs/common', '@nestjs/config', '@nestjs/core', 'joi', 'reflect-metadata', 'rxjs')
_DEV_DEPENDENCIES = ('@types/node', 'typescript')
_COUPLED = {'@nestjs/common': '@nestjs/core'}

_TSCONFIG = {
    'compilerOptions': {
        'module': 'commonjs', 'target': 'ES2022', 'outDir': 'dist', 'rootDir': 'src',
        'strict': True, 'noUncheckedIndexedAccess': True, 'exactOptionalPropertyTypes': True,
        'experimentalDecorators': True, 'emitDecoratorMetadata': True,
        'esModuleInterop': True, 'skipLibCheck': True,
    },
}

_MAIN = """\
import 'reflect-metadata';
import {{ Module }} from '@nestjs/common';
import {{ ConfigModule, ConfigService }} from '@nestjs/config';
import {{ NestFactory }} from '@nestjs/core';
{imports}

@Module({{
  imports: [
{for_root}
  ],
}})
class AppModule {{}}

async function main(): Promise<void> {{
  const app = await NestFactory.createApplicationContext(AppModule, {{ logger: ['error'] }});
  const config = app.get(ConfigService);
  process.stdout.write(JSON.stringify({{
    port: config.get<number>('PORT') ?? null,
    undeclared: config.get<string>('UNDECLARED_VAR') ?? null,
  }}));
  await app.close();
}}

main().catch((error: unknown): void => {{
  process.stderr.write(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}});
"""

_DATABASE_URL = 'postgresql://app@localhost:5432/app'
_CLEARED = ('NODE_ENV', 'PORT', 'DATABASE_URL', 'REDIS_URL', 'UNDECLARED_VAR')


def _section(rel: str, heading: str) -> str:
    with open(os.path.join(_SKILLS, rel), encoding='utf-8') as f:
        text = f.read()
    match = re.search(rf'^## {re.escape(heading)}\n(.*?)(?=^## |\Z)', text, re.DOTALL | re.MULTILINE)
    if not match:
        raise AssertionError(f'section "## {heading}" not found in {rel}')
    return match.group(1)


def _block(rel: str, heading: str, marker: str) -> str:
    blocks = [b for b in _FENCE_RE.findall(_section(rel, heading)) if marker in b]
    if len(blocks) != 1:
        raise AssertionError(f'expected one typescript block with {marker!r} in {rel} "{heading}", found {len(blocks)}')
    return blocks[0]


def _variants() -> dict[str, dict]:
    nest = ('engineering-nestjs/SKILL.md', 'Configuration')
    hexagonal = ('engineering-arch-hexagonal/SKILL.md', 'Env validation')
    return {
        'nestjs': {
            'files': {
                'main.ts': _MAIN.format(imports="import Joi from 'joi';",
                                        for_root=_block(*nest, 'ConfigModule.forRoot')),
            },
            'required': {'NODE_ENV': 'development', 'DATABASE_URL': _DATABASE_URL,
                         'REDIS_URL': 'redis://localhost:6379'},
        },
        'hexagonal': {
            'files': {
                'config/env.validation.ts': _block(*hexagonal, 'export const envValidationSchema'),
                'main.ts': _MAIN.format(imports="import { envValidationSchema } from './config/env.validation';",
                                        for_root=_block(*hexagonal, 'ConfigModule.forRoot')),
            },
            'required': {'NODE_ENV': 'development', 'DATABASE_URL': _DATABASE_URL},
        },
    }


@unittest.skipUnless(os.environ.get('PLAYBOOK_TEMPLATE_IT') == '1',
                     'opt-in: PLAYBOOK_TEMPLATE_IT=1 (needs node, npm and network)')
class TestNestConfigTemplate(unittest.TestCase):

    app_dir = ''
    variants: dict[str, dict] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_dir = tempfile.mkdtemp(prefix='nest_config_it_')
        cls.variants = _variants()
        package = package_json('nest-config-template-it', _DEPENDENCIES, _DEV_DEPENDENCIES, _COUPLED)
        files = {'package.json': json.dumps(package, indent=2), 'tsconfig.json': json.dumps(_TSCONFIG, indent=2)}
        for name, variant in cls.variants.items():
            files.update({f'src/{name}/{rel}': content for rel, content in variant['files'].items()})
        for rel, content in files.items():
            path = os.path.join(cls.app_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        try:
            cls._run(['npm', 'install', '--no-audit', '--no-fund', '--loglevel=error'])
            installed = subprocess.run(['npm', 'ls', '--depth=0'], cwd=cls.app_dir, capture_output=True,
                                       text=True, timeout=60, check=False)
            sys.stderr.write(f'\nInstalled versions:\n{installed.stdout}\n')
            cls._run(['npx', 'tsc', '-p', 'tsconfig.json'])
        except BaseException:
            cls.tearDownClass()  # unittest skips tearDownClass when setUpClass raises
            raise

    @classmethod
    def _run(cls, command: list[str]) -> None:
        done = subprocess.run(command, cwd=cls.app_dir, capture_output=True, text=True, timeout=600, check=False)
        if done.returncode != 0:
            raise AssertionError(f'{" ".join(command)} failed:\n{done.stdout}\n{done.stderr}')

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.app_dir, ignore_errors=True)

    def _boot(self, variant: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        full_env = {k: v for k, v in os.environ.items() if k not in _CLEARED}
        full_env.update(env)
        return subprocess.run(['node', f'dist/{variant}/main.js'], cwd=self.app_dir, env=full_env,
                              capture_output=True, text=True, timeout=_RUN_TIMEOUT_S, check=False)

    def _assert_booted(self, done: subprocess.CompletedProcess) -> dict:
        self.assertEqual(done.returncode, 0, f'the app did not boot:\n{done.stdout}\n{done.stderr[-3000:]}')
        return json.loads(done.stdout)

    def test_the_app_boots_beside_the_machine_variables_and_applies_defaults(self) -> None:
        self.assertIn('PATH', os.environ)
        for name, variant in self.variants.items():
            with self.subTest(variant=name):
                served = self._assert_booted(self._boot(name, variant['required']))
                self.assertEqual(served['port'], _DEFAULT_PORT)

    def test_an_undeclared_variable_is_not_served(self) -> None:
        for name, variant in self.variants.items():
            with self.subTest(variant=name):
                served = self._assert_booted(self._boot(name, {**variant['required'], 'UNDECLARED_VAR': 'x'}))
                self.assertIsNone(served['undeclared'])

    def test_every_missing_required_variable_is_named_at_once(self) -> None:
        for name, variant in self.variants.items():
            with self.subTest(variant=name):
                env = {k: v for k, v in variant['required'].items() if k not in ('NODE_ENV', 'DATABASE_URL')}
                done = self._boot(name, env)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn('"NODE_ENV" is required', done.stdout + done.stderr)
                self.assertIn('"DATABASE_URL" is required', done.stdout + done.stderr)

    def test_an_invalid_value_stops_the_boot(self) -> None:
        for name, variant in self.variants.items():
            with self.subTest(variant=name):
                done = self._boot(name, {**variant['required'], 'NODE_ENV': 'staging'})
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn('"NODE_ENV" must be one of', done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
