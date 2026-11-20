"""
Unit tests for the lab's identity mode: the personas file, the route catalog
read from the code, the identity client against a real local HTTP server, and
the report, which must never show a credential or a session token.

The run inside Docker is covered by tests/e2e/test_lab_integration.py, opt-in
with PLAYBOOK_LAB_IT=1.
"""
import copy
import http.server
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('lab_run', os.path.join(_ROOT, 'lab', 'run.py'))
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)
sys.path.insert(0, os.path.join(_ROOT, 'lab'))
import identities
import identity_client

PASSWORD = 'S3nha-da-Alice!'
TOKEN_PREFIX = 'tok-'
APP_ORIGIN = 'http://app.local'


def _persona(name: str, tenant: str, **over: object) -> dict:
    persona = {
        'name': name, 'role': 'customer', 'tenant': tenant,
        'login': {'method': 'POST', 'path': '/api/v1/auth/login',
                  'body': {'document': f'doc-{name}', 'password': f'{PASSWORD}{name}'}},
        'token': {'json': '/data/accessToken'},
        'send': {'header': 'Authorization', 'scheme': 'Bearer'},
    }
    persona.update(over)
    return persona


def _personas() -> list[dict]:
    return [_persona('alice', '001'), _persona('bob', '001'), _persona('carol', '002')]


class _FileCase(unittest.TestCase):

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix='pb_proj_')
        self.outside = tempfile.mkdtemp(prefix='pb_personas_')
        self.addCleanup(shutil.rmtree, self.project)
        self.addCleanup(shutil.rmtree, self.outside)

    def write(self, document: object, inside: bool = False) -> str:
        path = os.path.join(self.project if inside else self.outside, 'personas.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(document, fh)
        return path

    def load(self, personas: list[dict]) -> list[dict]:
        return identities.load_identities(self.write({'personas': personas}), self.project)


class TestPersonasFile(_FileCase):

    def test_a_valid_file_is_loaded(self):
        self.assertEqual([p['name'] for p in self.load(_personas())], ['alice', 'bob', 'carol'])

    def test_a_file_inside_the_project_is_refused(self):
        path = self.write({'personas': _personas()}, inside=True)
        with self.assertRaisesRegex(identities.IdentityError, 'inside the project'):
            identities.load_identities(path, self.project)

    def test_only_customers_are_accepted(self):
        for role in ('employee', 'operator', 'admin', None):
            with self.subTest(role=role):
                personas = _personas()
                personas[0]['role'] = role
                with self.assertRaisesRegex(identities.IdentityError, 'only as an ordinary customer'):
                    self.load(personas)

    def test_two_customers_of_one_tenant_and_one_of_another_are_required(self):
        for tenants in (('001', '001', '001'), ('001', '002', '003')):
            with self.subTest(tenants=tenants):
                personas = [_persona(n, t) for n, t in zip(('a', 'b', 'c'), tenants)]
                with self.assertRaisesRegex(identities.IdentityError, 'same tenant'):
                    self.load(personas)

    def test_a_login_path_naming_another_host_is_refused(self):
        for path in ('http://evil.example/login', '//evil.example/login', 'login', '/a@b/login'):
            with self.subTest(path=path):
                personas = _personas()
                personas[0]['login']['path'] = path
                with self.assertRaisesRegex(identities.IdentityError, 'not another host'):
                    self.load(personas)

    def test_token_and_send_forms(self):
        personas = _personas()
        personas[1]['token'] = {'cookie': 'session'}
        personas[1]['send'] = {'cookie': 'session'}
        personas[2]['token'] = {'json': ''}
        personas[2]['send'] = {'header': 'X-Api-Token'}
        self.assertEqual(len(self.load(personas)), 3)

    def test_unusable_token_and_send_forms_are_refused(self):
        bad = [('token', {'json': 'data.accessToken'}), ('token', {'json': '/a', 'cookie': 'b'}),
               ('send', {'header': 'Host'}), ('send', {'header': 'Authorization', 'extra': 'x'}),
               ('send', {'header': 'Bad Header'}), ('send', {'cookie': 'a;b'})]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                personas = _personas()
                personas[0][key] = value
                with self.assertRaises(identities.IdentityError):
                    self.load(personas)

    def test_cookie_names_follow_rfc_6265(self):
        for name in ('access_token', '__Host-session', 'sid.v2'):
            with self.subTest(name=name):
                personas = _personas()
                personas[0]['token'] = {'cookie': name}
                personas[0]['send'] = {'cookie': name}
                self.assertEqual(len(self.load(personas)), 3)
        for name in ('a;b', 'a b', 'a=b', 'a,b', ''):
            with self.subTest(name=name):
                personas = _personas()
                personas[0]['token'] = {'cookie': name}
                with self.assertRaisesRegex(identities.IdentityError, '"token"'):
                    self.load(personas)

    def test_public_login_keys_must_name_body_keys(self):
        personas = _personas()
        personas[0]['login']['body']['kind'] = 'customer'
        personas[0]['login']['public'] = ['kind']
        self.assertEqual(len(self.load(personas)), 3)
        for public in (['missing'], 'kind', [1], ['password']):
            with self.subTest(public=public):
                personas = _personas()
                personas[0]['login']['public'] = public
                with self.assertRaisesRegex(identities.IdentityError, 'login "public"'):
                    self.load(personas)

    def test_login_headers_are_accepted(self):
        personas = _personas()
        personas[0]['login']['headers'] = {'Origin': 'http://app.local', 'X-Client': 'mobile'}
        self.assertEqual(self.load(personas)[0]['login']['headers']['Origin'], 'http://app.local')

    def test_unusable_login_headers_are_refused(self):
        bad = [['Origin'], {'Host': 'evil.example'}, {'Content-Length': '0'}, {'Bad Header': 'x'},
               {'Origin': 'http://a\r\nX-Injected: 1'}, {'Origin': 1}, {'Origin': ''}]
        for headers in bad:
            with self.subTest(headers=headers):
                personas = _personas()
                personas[0]['login']['headers'] = headers
                with self.assertRaisesRegex(identities.IdentityError, 'login "headers"'):
                    self.load(personas)

    def test_duplicate_names_are_refused(self):
        personas = _personas()
        personas[1]['name'] = 'alice'
        with self.assertRaisesRegex(identities.IdentityError, 'unique'):
            self.load(personas)

    def test_invalid_json_says_where(self):
        path = os.path.join(self.outside, 'broken.json')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{"personas": [\n')
        with self.assertRaisesRegex(identities.IdentityError, 'not valid JSON'):
            identities.load_identities(path, self.project)


class TestCatalog(unittest.TestCase):

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix='pb_nest_')
        self.addCleanup(shutil.rmtree, self.project)
        os.makedirs(os.path.join(self.project, 'src'))
        with open(os.path.join(self.project, 'src', 'accounts.controller.ts'), 'w') as fh:
            fh.write("import { Controller, Get, Patch, Post } from '@nestjs/common';\n"
                     "@Controller('accounts')\nexport class AccountsController {\n"
                     "  @Get() list() {}\n  @Get(':id') one() {}\n"
                     "  @Patch(':id') remove() {}\n  @Post(':id/cash-deposits') deposit() {}\n}\n")

    def test_routes_come_from_the_declarations_whatever_the_verb_does(self):
        catalog, unknown = identities.build_catalog(self.project)
        self.assertEqual([(r['method'], r['path']) for r in catalog], [
            ('GET', '/accounts'), ('GET', '/accounts/{id}'), ('PATCH', '/accounts/{id}'),
            ('POST', '/accounts/{id}/cash-deposits')])
        self.assertEqual(unknown, [])

    def test_a_declaration_the_reader_cannot_read_is_reported(self):
        with open(os.path.join(self.project, 'src', 'dyn.controller.ts'), 'w') as fh:
            fh.write("@Controller(PREFIX)\nexport class Dyn { @Get() x() {} }\n")
        _, unknown = identities.build_catalog(self.project)
        self.assertTrue(any(u['file'].endswith('dyn.controller.ts') for u in unknown))


class TestPrefixAndDivergence(unittest.TestCase):

    CATALOG = ({'method': 'GET', 'path': '/accounts', 'file': 'a.ts', 'line': 1},
               {'method': 'GET', 'path': '/accounts/{id}', 'file': 'a.ts', 'line': 2},
               {'method': 'POST', 'path': '/auth/login', 'file': 'b.ts', 'line': 3})

    def test_the_prefix_is_read_from_the_document(self):
        ops = [{'method': 'GET', 'path': '/api/v1/accounts'},
               {'method': 'GET', 'path': '/api/v1/accounts/{accountId}'},
               {'method': 'POST', 'path': '/api/v1/auth/login'}]
        self.assertEqual(identity_client.infer_prefix(self.CATALOG, ops), '/api/v1')

    def test_no_prefix_when_the_document_matches_the_code(self):
        ops = [{'method': 'GET', 'path': '/accounts'}, {'method': 'POST', 'path': '/auth/login'}]
        self.assertEqual(identity_client.infer_prefix(self.CATALOG, ops), '')

    def test_routes_in_one_source_and_not_the_other_are_listed(self):
        ops = [{'method': 'GET', 'path': '/api/accounts'},
               {'method': 'DELETE', 'path': '/api/accounts/{id}'}]
        code_only, doc_only = identities.compare(self.CATALOG, ops, '/api')
        self.assertEqual([(r['method'], r['path']) for r in code_only],
                         [('GET', '/accounts/{id}'), ('POST', '/auth/login')])
        self.assertEqual(doc_only, [{'method': 'DELETE', 'path': '/api/accounts/{id}'}])


class TestCoverage(unittest.TestCase):

    def test_a_route_under_a_list_that_returned_resources_is_covered(self):
        catalog = [{'method': 'GET', 'path': '/accounts/{id}/statement', 'file': 'a', 'line': 1},
                   {'method': 'GET', 'path': '/cards/{id}', 'file': 'a', 'line': 2},
                   {'method': 'GET', 'path': '/loans/{id}', 'file': 'a', 'line': 3},
                   {'method': 'GET', 'path': '/accounts', 'file': 'a', 'line': 4}]
        lists = {'/accounts': {'alice': {'status': 200, 'count': 2}, 'bob': {'status': 200, 'count': 0}},
                 '/loans': {'alice': {'status': 403, 'count': 0}}}
        covered, uncovered = identities.coverage(catalog, lists, _personas())
        self.assertEqual([(r['path'], r['owners']) for r in covered],
                         [('/accounts/{id}/statement', ['alice'])])
        reasons = {r['path']: r['reason'] for r in uncovered}
        self.assertIn('no list route GET /cards', reasons['/cards/{id}'])
        self.assertIn('status 403', reasons['/loans/{id}'])


class _App(http.server.BaseHTTPRequestHandler):
    """A small API: an OpenAPI document, a JSON login, a cookie login, a list
    route that needs the session, and a redirect the client must not follow."""

    def _send(self, status: int, body: object, headers: tuple = ()) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        for key, value in headers:
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _who(self) -> str | None:
        auth = self.headers.get('Authorization') or ''
        cookie = self.headers.get('Cookie') or ''
        for value in (auth.removeprefix('Bearer '), cookie.removeprefix('session=')):
            if value.startswith(TOKEN_PREFIX):
                return value[len(TOKEN_PREFIX):]
        return None

    def do_GET(self):
        if self.path == '/api-json':
            self._send(200, {'openapi': '3.0.0', 'paths': {
                '/api/v1/accounts': {'get': {}}, '/api/v1/accounts/{id}': {'get': {}},
                '/api/v1/auth/login': {'post': {}}, '/api/v1/hidden': {'get': {}}}})
        elif self.path == '/api/v1/accounts':
            who = self._who()
            if who is None:
                self._send(401, {'error': 'unauthorized'})
            else:
                self._send(200, {'data': {'items': [{'id': f'{who}-1'}, {'id': f'{who}-2'}]}})
        elif self.path == '/api/v1/moved':
            self._send(302, {}, (('Location', 'http://elsewhere.invalid/'),))
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        who = body.get('document', '').removeprefix('doc-') or None
        ok = who is not None and body.get('password') == f'{PASSWORD}{who}'
        if self.path == '/api/v1/auth/origin-checked' and self.headers.get('Origin') != APP_ORIGIN:
            self._send(403, {'error': 'origin not allowed'})
        elif self.path in ('/api/v1/auth/login', '/api/v1/auth/origin-checked') and ok:
            self._send(200, {'data': {'accessToken': f'{TOKEN_PREFIX}{who}'}})
        elif self.path == '/api/v1/auth/session' and ok:
            # The session cookie is not the last Set-Cookie: each one must be read.
            self._send(200, {}, (('Set-Cookie', f'session={TOKEN_PREFIX}{who}; HttpOnly'),
                                 ('Set-Cookie', 'theme=dark; Path=/')))
        else:
            self._send(401, {'error': 'invalid credentials'})

    def log_message(self, *args):
        pass


class TestIdentityClient(unittest.TestCase):

    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _App)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.catalog = [{'method': 'GET', 'path': '/accounts', 'file': 'a', 'line': 1},
                        {'method': 'GET', 'path': '/accounts/{id}', 'file': 'a', 'line': 2},
                        {'method': 'POST', 'path': '/auth/login', 'file': 'b', 'line': 3}]

    def _run(self, personas: list[dict], prefix: str | None = None) -> dict:
        return identity_client.run({'base_url': self.base, 'prefix': prefix,
                                    'openapi_paths': ['/openapi.json', '/api-json'],
                                    'catalog': self.catalog, 'personas': personas})

    def test_signs_in_lists_what_each_owns_and_reads_the_prefix(self):
        result = self._run(_personas())
        self.assertEqual(result['openapi_path'], '/api-json')
        self.assertEqual(result['prefix'], '/api/v1')
        self.assertTrue(all(s['signed_in'] for s in result['sessions']))
        self.assertEqual(result['lists']['/accounts']['alice'], {'status': 200, 'count': 2})

    def test_a_cookie_session_is_carried_as_a_cookie(self):
        personas = _personas()
        personas[0]['login']['path'] = '/api/v1/auth/session'
        personas[0]['token'] = {'cookie': 'session'}
        personas[0]['send'] = {'cookie': 'session'}
        result = self._run(personas)
        self.assertEqual(result['lists']['/accounts']['alice']['count'], 2)

    def test_login_headers_reach_a_login_that_checks_the_origin(self):
        personas = _personas()
        for persona in personas:
            persona['login']['path'] = '/api/v1/auth/origin-checked'
        personas[0]['login']['headers'] = {'Origin': APP_ORIGIN}
        result = self._run(personas)
        status = {s['persona']: (s['signed_in'], s['status']) for s in result['sessions']}
        self.assertEqual(status['alice'], (True, 200))
        self.assertEqual(status['bob'], (False, 403))
        self.assertEqual(result['lists']['/accounts']['alice']['count'], 2)

    def test_a_wrong_password_is_reported_and_lists_nothing_for_that_persona(self):
        personas = _personas()
        personas[2]['login']['body']['password'] = 'wrong'
        result = self._run(personas)
        carol = next(s for s in result['sessions'] if s['persona'] == 'carol')
        self.assertEqual((carol['signed_in'], carol['status']), (False, 401))
        self.assertNotIn('carol', result['lists']['/accounts'])

    def test_the_result_holds_no_credential_and_no_token(self):
        text = json.dumps(self._run(_personas()))
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(TOKEN_PREFIX, text)

    def test_a_redirect_is_returned_not_followed(self):
        status, headers, _ = identity_client.request(self.base, 'GET', '/api/v1/moved')
        self.assertEqual(status, 302)
        self.assertEqual(dict(headers).get('Location'), 'http://elsewhere.invalid/')

    def test_an_unreachable_target_is_status_zero(self):
        self.assertEqual(identity_client.request('http://127.0.0.1:1', 'GET', '/')[0], 0)

    def test_a_target_that_never_answers_is_reported_unreachable_and_nothing_is_sent(self):
        with mock.patch.object(identity_client, 'sign_in') as sign_in:
            result = identity_client.run({'base_url': 'http://127.0.0.1:1', 'prefix': None,
                                          'openapi_paths': ['/api-json'], 'catalog': self.catalog,
                                          'personas': _personas(), 'ready_seconds': 0})
        self.assertEqual((result['reachable'], result['sessions']), (False, []))
        sign_in.assert_not_called()

    def test_the_client_waits_for_a_target_that_starts_late(self):
        answers = iter([(0, [], b''), (502, [], b''), (503, [], b''), (404, [], b'')])
        with mock.patch.object(identity_client, 'request', side_effect=lambda *a, **k: next(answers)), \
                mock.patch.object(identity_client.time, 'sleep'):
            self.assertIsNotNone(identity_client.wait_until_answering(self.base, 60))


class TestReport(unittest.TestCase):

    def test_the_report_names_the_routes_and_never_a_credential(self):
        personas = _personas()
        catalog = [{'method': 'GET', 'path': '/accounts', 'file': 'a.ts', 'line': 1},
                   {'method': 'GET', 'path': '/accounts/{id}', 'file': 'a.ts', 'line': 2},
                   {'method': 'GET', 'path': '/cards/{id}', 'file': 'c.ts', 'line': 9}]
        result = {'openapi_path': '/api-json', 'prefix': '/api/v1',
                  'operations': [{'method': 'GET', 'path': '/api/v1/accounts'},
                                 {'method': 'GET', 'path': '/api/v1/accounts/{id}'},
                                 {'method': 'DELETE', 'path': '/api/v1/hidden'}],
                  'sessions': [{'persona': p['name'], 'status': 200, 'signed_in': True} for p in personas],
                  'lists': {'/accounts': {'alice': {'status': 200, 'count': 1}}}}
        unknown = [{'file': 'dyn.ts', 'line': 4, 'reason': f'prefix is not a literal {PASSWORD}alice'}]
        text = identities.format_report('http://app:3000', catalog, unknown, result, personas)
        self.assertIn('missing from the document: GET /api/v1/cards/{id} (c.ts:9)', text)
        self.assertIn('no declaration in the code read: DELETE /api/v1/hidden', text)
        self.assertIn('not covered: GET /api/v1/cards/{id}', text)
        self.assertIn('dyn.ts:4', text)
        self.assertNotIn(PASSWORD, text)


class TestRedaction(unittest.TestCase):

    def test_short_login_values_are_left_so_the_report_stays_readable(self):
        persona = _persona('alice', '001')
        persona['login']['body']['kind'] = 'pf'
        found = identities.secrets_of([persona])
        self.assertIn(f'{PASSWORD}alice', found)
        self.assertNotIn('pf', found)
        self.assertEqual(identities.redact('3 customers, kind pf', found), '3 customers, kind pf')

    def test_public_login_values_stay_readable_and_the_password_does_not(self):
        persona = _persona('alice', '001')
        persona['login']['body']['kind'] = 'customer'
        persona['login']['public'] = ['kind']
        found = identities.secrets_of([persona])
        self.assertEqual(identities.redact(f'3 customers via {PASSWORD}alice', found),
                         '3 customers via ***')

    def test_login_header_values_are_redacted(self):
        persona = _persona('alice', '001')
        persona['login']['headers'] = {'X-Device-Key': 'dev-key-123'}
        found = identities.secrets_of([persona])
        self.assertEqual(identities.redact('sent dev-key-123', found), 'sent ***')


class TestLabWiring(unittest.TestCase):

    def setUp(self):
        self.report_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.report_dir)

    def test_a_target_that_never_answered_fails_with_its_log_not_as_a_failed_login(self):
        personas = _personas()
        project_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project_root)
        os.makedirs(os.path.join(project_root, 'src'))
        with open(os.path.join(project_root, 'src', 'a.controller.ts'), 'w') as fh:
            fh.write("@Controller('accounts')\nexport class A { @Get() list() {} }\n")
        calls = []

        def compose(project, config_file, *args, **kwargs):
            calls.append(args)
            if 'run' in args:
                with open(os.path.join(self.report_dir, lab.CLIENT_RESULT), 'w') as fh:
                    json.dump({'reachable': False}, fh)
                return lab.subprocess.CompletedProcess(args, 0, '', '')
            return lab.subprocess.CompletedProcess(
                args, 0, f'GET https://registry.npmjs.org error EAI_AGAIN {PASSWORD}alice\n', '')

        args = lab.argparse.Namespace(openapi_path=None, api_prefix=None)
        with mock.patch.object(lab, '_compose', side_effect=compose):
            with self.assertRaises(lab.LabError) as raised:
                lab._run_identities(lab.parse_target('app', 3000, '/'), 'redlab-x', project_root,
                                    'cfg.json', self.report_dir, args, personas)
        message = str(raised.exception)
        self.assertIn('never answered', message)
        self.assertIn('EAI_AGAIN', message)
        self.assertNotIn(f'{PASSWORD}alice', message)
        self.assertTrue(any('logs' in call for call in calls))

    def test_the_client_runs_locked_down_on_the_internal_network_only(self):
        base = {'services': {'app': {'build': {'context': '/work/shop'}, 'ports': [
            {'target': 3000, 'published': '3000'}]}}}
        target = lab.parse_target('app', 3000, '/')
        config = lab.build_lab_config(copy.deepcopy(base), target, 'redlab-shop', self.report_dir,
                                      2, tool='identities')
        client = config['services'][lab.SCANNER]
        self.assertEqual(client['image'], lab.CLIENT_IMAGE)
        self.assertEqual(client['command'], ['python3', '-I', f'{lab.REPORT_MOUNT}/identity_client.py'])
        self.assertEqual(lab.validate_lab_config(config, 'app', self.report_dir, '/work/shop',
                                                 lab.CLIENT_IMAGE), [])
        self.assertIn('the scanner image is not the pinned one',
                      lab.validate_lab_config(config, 'app', self.report_dir, '/work/shop'))

    def test_the_client_reads_the_directory_the_lab_mounts(self):
        self.assertEqual(identity_client.RUN_DIR, lab.REPORT_MOUNT)

    def test_the_client_image_is_the_base_sqlmap_builds_on(self):
        with open(lab.SQLMAP_DOCKERFILE, encoding='utf-8') as fh:
            base = re.search(r'^FROM\s+(\S+)', fh.read(), re.MULTILINE).group(1)
        self.assertEqual(base, lab.CLIENT_IMAGE)


class TestIdentityArguments(_FileCase):

    def _args(self, **over: object):
        base = {'service': None, 'port': None, 'path': '/', 'project_dir': self.project,
                'compose_file': [], 'minutes': 2, 'startup_seconds': 0, 'active': False,
                'active_minutes': lab.ACTIVE_SCAN_MINUTES, 'sqlmap': False, 'endpoint': None,
                'level': lab.SQLMAP_LEVEL, 'risk': lab.SQLMAP_RISK, 'time_sec': lab.SQLMAP_TIME_SEC,
                'gate': False, 'commit': None, 'identities': self.write({'personas': _personas()}),
                'openapi_path': None, 'api_prefix': None}
        base.update(over)
        return lab.argparse.Namespace(**base)

    def _refused(self, args, pattern: str) -> None:
        with mock.patch.object(lab, 'check_docker') as docker:
            with self.assertRaisesRegex(lab.LabError, pattern):
                lab.scan(args)
            docker.assert_not_called()

    def test_identity_mode_runs_alone(self):
        for flag in ('sqlmap', 'active', 'gate'):
            with self.subTest(flag=flag):
                self._refused(self._args(**{flag: True}), 'runs on its own')

    def test_a_prefix_or_document_path_naming_another_host_is_refused(self):
        for flag in ('api_prefix', 'openapi_path'):
            for value in ('http://evil.example', '//evil.example', '/a@b'):
                with self.subTest(flag=flag, value=value):
                    self._refused(self._args(**{flag: value}), 'not another host')

    def test_a_personas_file_inside_the_project_is_refused_before_docker(self):
        args = self._args(identities=self.write({'personas': _personas()}, inside=True))
        self._refused(args, 'inside the project')


if __name__ == '__main__':
    unittest.main()
