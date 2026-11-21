"""
Real run of the engineering-nestjs-security templates.

Opt-in: set PLAYBOOK_TEMPLATE_IT=1. It needs node and npm with network access
(about a minute to install). It is skipped otherwise.

The code is taken from the skill's own code blocks (main.ts, the TRUST_PROXY
config, the throttler guard), not from a copy, so the test proves the text the
model reads. A minimal AppModule around them is the only code written here. The
app is compiled under `strict` and started, and every claim the skill makes
about the proxy trust and the CSP is checked over HTTP.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILL = os.path.join(_ROOT, 'skills', 'engineering-nestjs-security', 'SKILL.md')
_FENCE_RE = re.compile(r'^```typescript\n(.*?)^```', re.DOTALL | re.MULTILINE)
_BOOT_TIMEOUT_S = 30.0
_THROTTLE_LIMIT = 30  # ThrottlerModule.forRoot in the skill's AppModule additions

_PACKAGE = {
    'name': 'nest-security-template-it',
    'private': True,
    'dependencies': {
        '@fastify/helmet': '^13.0.0',
        '@fastify/static': '^10.1.2',
        '@nestjs/common': '^11.0.0',
        '@nestjs/config': '^4.0.0',
        '@nestjs/core': '^11.0.0',
        '@nestjs/platform-fastify': '^11.0.0',
        '@nestjs/swagger': '^11.0.0',
        '@nestjs/throttler': '^6.0.0',
        'class-transformer': '^0.5.0',
        'class-validator': '^0.14.0',
        'joi': '^17.0.0',
        'nestjs-pino': '^4.0.0',
        'pino': '^9.0.0',
        'pino-http': '^10.0.0',
        'reflect-metadata': '^0.2.0',
        'rxjs': '^7.8.0',
    },
    'devDependencies': {'@types/node': '^22.0.0', 'typescript': '^5.6.0'},
}

_TSCONFIG = {
    'compilerOptions': {
        'module': 'commonjs', 'target': 'ES2022', 'outDir': 'dist', 'rootDir': 'src',
        'strict': True, 'experimentalDecorators': True, 'emitDecoratorMetadata': True,
        'esModuleInterop': True, 'skipLibCheck': True,
    },
}

_APP_MODULE = """\
import { Controller, Get, Module, Req } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { ConfigModule } from '@nestjs/config';
import { ThrottlerModule } from '@nestjs/throttler';
import { LoggerModule } from 'nestjs-pino';
import * as Joi from 'joi';
import type { FastifyRequest } from 'fastify';
import { ThrottlerBehindProxyGuard } from '../../common/guards/throttler-behind-proxy.guard';
import { trustProxyEnvSchema } from '../../config/trust-proxy.config';

@Controller('echo')
class EchoController {
  @Get()
  ip(@Req() request: FastifyRequest): { ip: string } {
    return { ip: request.ip };
  }
}

@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      validationSchema: Joi.object({
        NODE_ENV: Joi.string().valid('development', 'production', 'test').required(),
        PORT: Joi.number().default(3000),
        CORS_ORIGIN: Joi.string().default(''),
        TRUST_PROXY: trustProxyEnvSchema,
      }),
      validationOptions: { allowUnknown: true, abortEarly: false },
    }),
    LoggerModule.forRoot({ pinoHttp: { level: 'warn' } }),
    ThrottlerModule.forRoot([{ ttl: 60_000, limit: 30 }]),
  ],
  controllers: [EchoController],
  providers: [{ provide: APP_GUARD, useClass: ThrottlerBehindProxyGuard }],
})
export class AppModule {}
"""


def _skill_block(marker: str) -> str:
    with open(_SKILL, encoding='utf-8') as f:
        blocks = [b for b in _FENCE_RE.findall(f.read()) if marker in b]
    if len(blocks) != 1:
        raise AssertionError(f'expected one typescript block with {marker!r} in the skill, found {len(blocks)}')
    return blocks[0]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read().decode()


@unittest.skipUnless(os.environ.get('PLAYBOOK_TEMPLATE_IT') == '1',
                     'opt-in: PLAYBOOK_TEMPLATE_IT=1 (needs node, npm and network)')
class TestNestSecurityTemplate(unittest.TestCase):

    app_dir = ''

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_dir = tempfile.mkdtemp(prefix='nest_security_it_')
        files = {
            'package.json': json.dumps(_PACKAGE, indent=2),
            'tsconfig.json': json.dumps(_TSCONFIG, indent=2),
            'src/main.ts': _skill_block('async function bootstrap'),
            'src/config/trust-proxy.config.ts': _skill_block('export function readTrustProxy'),
            'src/common/guards/throttler-behind-proxy.guard.ts': _skill_block('class ThrottlerBehindProxyGuard'),
            'src/presentation/modules/app.module.ts': _APP_MODULE,
        }
        for rel, content in files.items():
            path = os.path.join(cls.app_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        for command in (['npm', 'install', '--no-audit', '--no-fund', '--loglevel=error'],
                        ['npx', 'tsc', '-p', 'tsconfig.json']):
            done = subprocess.run(command, cwd=cls.app_dir, capture_output=True, text=True,
                                  timeout=600, check=False)
            if done.returncode != 0:
                raise AssertionError(f'{" ".join(command)} failed:\n{done.stdout}\n{done.stderr}')

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.app_dir, ignore_errors=True)

    def setUp(self) -> None:
        env_file = os.path.join(self.app_dir, '.env')
        if os.path.exists(env_file):
            os.remove(env_file)

    def _start(self, **env: str) -> tuple[subprocess.Popen, int]:
        port = _free_port()
        full_env = {k: v for k, v in os.environ.items() if k not in ('NODE_ENV', 'TRUST_PROXY')}
        full_env.update({'PORT': str(port), **env})
        process = subprocess.Popen(['node', 'dist/main.js'], cwd=self.app_dir, env=full_env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, process)
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return process, port
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                    return process, port
            except OSError:
                time.sleep(0.2)
        raise AssertionError('the app neither listened nor exited')

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    def _assert_up(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            output = (process.stdout.read() if process.stdout else '') + (process.stderr.read() if process.stderr else '')
            self.fail(f'the app exited with {process.returncode}:\n{output[-3000:]}')

    def _assert_refused(self, process: subprocess.Popen, text: str) -> None:
        self.assertIsNotNone(process.wait(timeout=_BOOT_TIMEOUT_S))
        self.assertNotEqual(process.returncode, 0)
        output = (process.stdout.read() if process.stdout else '') + (process.stderr.read() if process.stderr else '')
        self.assertIn(text, output)

    def test_a_forged_forwarded_for_does_not_change_the_client_ip(self) -> None:
        process, port = self._start(NODE_ENV='development')
        self._assert_up(process)
        statuses = []
        for i in range(_THROTTLE_LIMIT + 1):
            status, _, body = _get(port, '/api/v1/echo', {'X-Forwarded-For': f'203.0.113.{i + 1}'})
            statuses.append(status)
            if status == 200:
                self.assertEqual(json.loads(body)['ip'], '127.0.0.1')
        self.assertEqual(statuses[:_THROTTLE_LIMIT], [200] * _THROTTLE_LIMIT)
        self.assertEqual(statuses[-1], 429, 'a new forged IP per request escaped the rate limit')

    def test_a_hop_count_takes_the_address_the_trusted_proxy_added(self) -> None:
        process, port = self._start(NODE_ENV='development', TRUST_PROXY='1')
        self._assert_up(process)
        status, _, body = _get(port, '/api/v1/echo', {'X-Forwarded-For': '203.0.113.7, 198.51.100.9'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['ip'], '198.51.100.9')

    def test_trust_proxy_true_stops_the_boot(self) -> None:
        process, _ = self._start(NODE_ENV='development', TRUST_PROXY='true')
        self._assert_refused(process, 'TRUST_PROXY=true')

    def test_trust_proxy_true_in_the_env_file_stops_the_boot(self) -> None:
        with open(os.path.join(self.app_dir, '.env'), 'w') as f:
            f.write('TRUST_PROXY=true\n')
        process, _ = self._start(NODE_ENV='development')
        self._assert_refused(process, 'TRUST_PROXY=true')

    def test_production_requires_an_explicit_trust_proxy(self) -> None:
        process, _ = self._start(NODE_ENV='production')
        self._assert_refused(process, 'TRUST_PROXY')

    def test_api_routes_carry_a_strict_csp_in_every_environment(self) -> None:
        for env in ({'NODE_ENV': 'development'}, {'NODE_ENV': 'production', 'TRUST_PROXY': 'false'}):
            with self.subTest(**env):
                process, port = self._start(**env)
                self._assert_up(process)
                _, headers, _ = _get(port, '/api/v1/echo')
                csp = headers.get('content-security-policy', '')
                self.assertIn("script-src 'self'", csp)
                self.assertNotIn('unsafe-inline', csp)
                self.assertNotIn('unsafe-eval', csp)
                self._stop(process)

    def test_docs_exist_only_in_development_with_scripts_kept_to_self(self) -> None:
        process, port = self._start(NODE_ENV='development')
        status, headers, html = _get(port, '/api/docs')
        self.assertEqual(status, 200)
        csp = headers.get('content-security-policy', '')
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotRegex(csp, r"script-src[^;]*unsafe")
        inline = [tag for tag in re.findall(r'<script\b[^>]*>', html) if 'src=' not in tag]
        self.assertEqual(inline, [], "Swagger UI serves an inline script that script-src 'self' blocks")
        self._stop(process)
        process, port = self._start(NODE_ENV='production', TRUST_PROXY='false')
        self.assertEqual(_get(port, '/api/docs')[0], 404)


if __name__ == '__main__':
    unittest.main()
