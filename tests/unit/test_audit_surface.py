"""
Unit tests for audit/surface.py: the reader that lists what an audit must cover.

One test per declaration form each reader accepts, and one per form it must
surface as unknown instead of dropping. The git paths run real git on a
temporary repository.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'audit'))
import surface
from surface import Seed

JAVA_CTRL = 'back/src/main/java/app/ConversationController.java'


class SurfaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def write(self, rel: str, text: str) -> None:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def routes(self) -> list[tuple[str, str]]:
        found, _ = surface.read_routes(surface.Tree(self.root))
        return sorted((r.method, r.path) for r in found)

    def unknown(self) -> list[str]:
        _, unknown = surface.read_routes(surface.Tree(self.root))
        return [u.reason for u in unknown]

    def spring(self, body: str, prefix: str = '@RequestMapping("/api/conversations")') -> None:
        self.write(JAVA_CTRL, f'package app;\n@RestController\n{prefix}\npublic class ConversationController {{\n'
                              f'{body}\n}}\n')


class SpringRoutes(SurfaceCase):
    def test_literal_path_joins_class_prefix(self) -> None:
        self.spring('@GetMapping("/{id}")\npublic X get() { return null; }')
        self.assertEqual(self.routes(), [('GET', '/api/conversations/{id}')])

    def test_mapping_without_arguments_uses_class_prefix(self) -> None:
        self.spring('@PostMapping\npublic X create() { return null; }')
        self.assertEqual(self.routes(), [('POST', '/api/conversations')])

    def test_value_and_path_attributes(self) -> None:
        self.spring('@PutMapping(value = "/{id}/pin")\nX a() {}\n@DeleteMapping(path = "/{id}/pin")\nX b() {}')
        self.assertEqual(self.routes(), [('DELETE', '/api/conversations/{id}/pin'),
                                         ('PUT', '/api/conversations/{id}/pin')])

    def test_array_of_paths(self) -> None:
        self.spring('@GetMapping({"/a", "/b"})\nX a() {}')
        self.assertEqual(self.routes(), [('GET', '/api/conversations/a'), ('GET', '/api/conversations/b')])

    def test_kotlin_array_of_paths(self) -> None:
        self.write('back/C.kt', '@RestController\n@RequestMapping("/k")\nclass C {\n'
                                '@GetMapping(value = ["/a", "/b"])\nfun a() {}\n}\n')
        self.assertEqual(self.routes(), [('GET', '/k/a'), ('GET', '/k/b')])

    def test_request_mapping_with_methods(self) -> None:
        self.spring('@RequestMapping(value = "/x", method = RequestMethod.PUT)\nX a() {}\n'
                    '@RequestMapping(value = "/y", method = {RequestMethod.GET, RequestMethod.POST})\nX b() {}\n'
                    '@RequestMapping("/z")\nX c() {}')
        self.assertEqual(self.routes(), [('ANY', '/api/conversations/z'), ('GET', '/api/conversations/y'),
                                         ('POST', '/api/conversations/y'), ('PUT', '/api/conversations/x')])

    def test_other_attribute_string_is_not_a_path(self) -> None:
        self.spring('@GetMapping(produces = "application/json")\nX a() {}')
        self.assertEqual(self.routes(), [('GET', '/api/conversations')])

    def test_multiline_arguments(self) -> None:
        self.spring('@PostMapping(\n    value = "/{id}/mute",\n    consumes = "application/json"\n)\nX a() {}')
        self.assertEqual(self.routes(), [('POST', '/api/conversations/{id}/mute')])

    def test_path_variable_regex_is_normalized(self) -> None:
        self.spring('@GetMapping("/{id:[0-9]+}")\nX a() {}')
        self.assertEqual(self.routes(), [('GET', '/api/conversations/{id}')])

    def test_mapping_in_interface_without_controller_annotation(self) -> None:
        self.write('back/Api.java', '@RequestMapping("/api")\npublic interface Api {\n@GetMapping("/ping")\nX ping();\n}\n')
        self.assertEqual(self.routes(), [('GET', '/api/ping')])

    def test_non_literal_method_path_is_unknown(self) -> None:
        self.spring('@GetMapping(Paths.ARCHIVE)\nX a() {}')
        self.assertEqual(self.routes(), [])
        self.assertIn('@GetMapping path is not a literal', self.unknown())

    def test_non_literal_class_prefix_is_unknown(self) -> None:
        self.spring('@GetMapping("/a")\nX a() {}', prefix='@RequestMapping(ApiPaths.BASE)')
        self.assertEqual(self.routes(), [])
        self.assertTrue(any('class-level @RequestMapping' in reason for reason in self.unknown()))

    def test_controller_with_no_mapping_is_unknown(self) -> None:
        self.write(JAVA_CTRL, '@RestController\npublic class C {\n@CustomRoute("/x")\nX a() {}\n}\n')
        self.assertIn('controller with no route mapping recognised', self.unknown())

    def test_controller_advice_is_not_a_controller(self) -> None:
        self.write('back/H.java', '@ControllerAdvice\npublic class H {}\n')
        self.assertEqual(self.unknown(), [])


class NestRoutes(SurfaceCase):
    def nest(self, controller: str, body: str) -> None:
        self.write('api/src/users.controller.ts', f"@Controller({controller})\nexport class UsersController {{\n{body}\n}}\n")

    def test_literal_prefix_and_param(self) -> None:
        self.nest("'users'", "@Get(':id')\nfind() {}")
        self.assertEqual(self.routes(), [('GET', '/users/{id}')])

    def test_empty_controller_and_method(self) -> None:
        self.nest('', '@Post()\ncreate() {}')
        self.assertEqual(self.routes(), [('POST', '/')])

    def test_object_controller_path(self) -> None:
        self.nest("{ path: 'admin/users', version: '1' }", "@Delete(':id')\nremove() {}")
        self.assertEqual(self.routes(), [('DELETE', '/admin/users/{id}')])

    def test_array_of_method_paths(self) -> None:
        self.nest("'users'", "@Get(['a', 'b'])\nlist() {}")
        self.assertEqual(self.routes(), [('GET', '/users/a'), ('GET', '/users/b')])

    def test_non_literal_method_path_is_unknown(self) -> None:
        self.nest("'users'", '@Get(ROUTES.list)\nlist() {}')
        self.assertIn('@Get path is not a literal', self.unknown())

    def test_non_literal_controller_is_unknown(self) -> None:
        self.nest('PREFIX', "@Get('a')\nlist() {}")
        self.assertEqual(self.routes(), [])
        self.assertTrue(any('@Controller path is not a literal' in reason for reason in self.unknown()))

    def test_param_decorators_are_not_routes(self) -> None:
        self.nest("'users'", "@Get()\nlist(@Headers() h: string, @Param('id') id: string) {}")
        self.assertEqual(self.routes(), [('GET', '/users')])


class ExpressRoutes(SurfaceCase):
    def express(self, body: str) -> None:
        self.write('api/routes.js', f"const express = require('express');\nconst router = express.Router();\n{body}\n")

    def test_literal_routes(self) -> None:
        self.express("router.get('/a', h);\napp.post(\"/b/:id\", auth, h);")
        self.assertEqual(self.routes(), [('GET', '/a'), ('POST', '/b/{id}')])

    def test_route_chain(self) -> None:
        self.express("router.route('/c/:id')\n  .get(h)\n  .put(h);")
        self.assertEqual(self.routes(), [('GET', '/c/{id}'), ('PUT', '/c/{id}')])

    def test_single_argument_get_is_not_a_route(self) -> None:
        self.express("const v = cache.get('key');\nrouter.get('/a', h);")
        self.assertEqual(self.routes(), [('GET', '/a')])

    def test_template_literal_is_unknown(self) -> None:
        self.express('router.get(`/v${version}/a`, h);')
        self.assertIn('route path is a template literal', self.unknown())

    def test_constant_path_is_unknown(self) -> None:
        self.express('router.get(PATHS.list, h);')
        self.assertIn('route path is not a literal', self.unknown())

    def test_regex_path_is_unknown(self) -> None:
        self.express('router.get(/^\\/a$/, h);')
        self.assertIn('route path is a regular expression', self.unknown())

    def test_file_without_router_import_is_ignored(self) -> None:
        self.write('web/store.ts', "store.get('/a', fallback);\n")
        self.assertEqual(self.routes(), [])


class ForeignStacks(SurfaceCase):
    def test_go_routes_are_reported_as_unread(self) -> None:
        self.write('svc/main.go', 'package main\nfunc main() { r.HandleFunc("/x", h) }\n')
        self.assertTrue(any('(go)' in reason for reason in self.unknown()))

    def test_python_decorator_routes_are_reported_as_unread(self) -> None:
        self.write('svc/app.py', '@app.get("/x")\ndef x(): pass\n')
        self.assertTrue(any('(python)' in reason for reason in self.unknown()))

    def test_django_path_only_in_urls_py(self) -> None:
        self.write('svc/util.py', 'p = path("x")\n')
        self.assertEqual(self.unknown(), [])
        self.write('svc/urls.py', 'urlpatterns = [path("x/", view)]\n')
        self.assertTrue(any('(python)' in reason for reason in self.unknown()))


SERVICE_IFACE = 'back/src/main/java/app/ConversationService.java'
SERVICE_IMPL = 'back/src/main/java/app/ConversationServiceImpl.java'
REPOSITORY = 'back/src/main/java/app/ConversationRepository.java'


class ModuleSurface(SurfaceCase):
    def module(self) -> None:
        self.spring('private final ConversationService service;\n'
                    '@PostMapping("/{conversationId}/pin")\nX pin() { return service.pin(); }')
        self.write(SERVICE_IFACE, 'public interface ConversationService { X pin(); }\n')
        self.write(SERVICE_IMPL, 'public class ConversationServiceImpl implements ConversationService {\n'
                                 'private final ConversationRepository repository;\n'
                                 '@Transactional\npublic X pin() {\n  return repository.save(x);\n}\n}\n')
        self.write(REPOSITORY, 'public interface ConversationRepository extends JpaRepository<C, Long> {\n'
                               '@Lock(LockModeType.PESSIMISTIC_WRITE)\nC findLocked(Long id);\n}\n')
        self.write('front/src/app/inbox/inbox.service.ts',
                   'pin(id: number) {\n  return this.http.post(`${this.base}/api/conversations/${id}/pin`, {});\n}\n'
                   "mute(id: number) {\n  return this.http.post(this.base + '/api/conversations/' + id + '/pin2', {});\n}\n")

    def test_prefix_seed_reaches_interface_implementation_and_repository(self) -> None:
        self.module()
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'))
        self.assertEqual([(r['method'], r['path']) for r in result['routes']],
                         [('POST', '/api/conversations/{conversationId}/pin')])
        self.assertTrue({JAVA_CTRL, SERVICE_IFACE, SERVICE_IMPL, REPOSITORY} <= set(result['files']))
        kinds = {(s['file'], s['kind']) for s in result['sites']}
        self.assertIn((SERVICE_IMPL, 'write'), kinds)
        self.assertIn((SERVICE_IMPL, 'transaction'), kinds)
        self.assertIn((REPOSITORY, 'lock'), kinds)

    def test_shared_base_class_does_not_widen_the_scope(self) -> None:
        self.module()
        self.write('back/src/main/java/app/BaseEntity.java', 'public abstract class BaseEntity {}\n')
        self.write('back/src/main/java/app/Conversation.java', 'public class Conversation extends BaseEntity {}\n')
        self.write(SERVICE_IMPL, 'public class ConversationServiceImpl implements ConversationService {\n'
                                 'Conversation c;\n}\n')
        self.write('back/src/main/java/app/Invoice.java', 'public class Invoice extends BaseEntity {}\n')
        self.write('back/src/main/kotlin/app/Order.kt', 'class Order(id: Long) : BaseEntity()\n')
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'), depth=3)
        self.assertIn('back/src/main/java/app/BaseEntity.java', result['files'])
        self.assertNotIn('back/src/main/java/app/Invoice.java', result['files'])
        self.assertNotIn('back/src/main/kotlin/app/Order.kt', result['files'])

    def test_kotlin_interface_implementation_is_reached(self) -> None:
        self.spring('private final Notifier notifier;\n@PostMapping("/{id}/mute")\nX mute() {}')
        self.write('back/Notifier.kt', 'interface Notifier { fun send() }\n')
        self.write('back/PushNotifier.kt', 'class PushNotifier(private val gw: Gateway) : Notifier, Base() {\n}\n')
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'))
        self.assertIn('back/PushNotifier.kt', result['files'])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads any folder')
    def test_unreadable_folder_is_listed_in_limits(self) -> None:
        self.module()
        locked = os.path.join(self.root, 'back', 'locked')
        os.makedirs(locked)
        os.chmod(locked, 0)
        try:
            result = surface.build(self.root, Seed(route_prefix='/api'))
        finally:
            os.chmod(locked, 0o755)
        self.assertTrue(any('could not be listed' in limit and 'back/locked' in limit for limit in result['limits']))

    def test_comment_lines_are_not_sites(self) -> None:
        self.module()
        self.write(SERVICE_IMPL, 'public class ConversationServiceImpl implements ConversationService {\n'
                                 '/** Keep @Transactional here and call repository.save(x) once. */\n'
                                 '// repository.delete(x) is done elsewhere\n}\n')
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'))
        self.assertEqual([s for s in result['sites'] if s['file'] == SERVICE_IMPL], [])

    def test_exact_caller_in_sibling_front_folder(self) -> None:
        self.module()
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'))
        exact = [c for c in result['callers'] if c['match'] == 'exact']
        self.assertEqual([(c['file'], c['line']) for c in exact], [('front/src/app/inbox/inbox.service.ts', 2)])

    def test_screens_using_the_caller_are_consumers(self) -> None:
        self.module()
        self.write('front/src/app/inbox/list.component.ts',
                   'export class ListComponent {\n  constructor(private inbox: InboxService) {}\n}\n')
        self.write('front/src/app/inbox/list.component.spec.ts', 'const s = new InboxService();\n')
        self.write('front/src/app/inbox/inbox.service.ts',
                   'export class InboxService implements OnDestroy {\n  pin(id: number) {\n'
                   '    return this.http.post(`/api/conversations/${id}/pin`, {});\n  }\n}\n')
        self.write('front/src/app/other/unrelated.component.ts',
                   'export class UnrelatedComponent implements OnDestroy {}\n')
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'))
        self.assertEqual(result['consumers'], ['front/src/app/inbox/list.component.ts'])

    def test_concatenated_caller_is_loose(self) -> None:
        self.spring('@GetMapping("/{id}/history")\nX a() {}')
        self.write('front/a.ts', "get(id) { return http.get(BASE + '/api/conversations/' + id + '/history'); }\n")
        result = surface.build(self.root, Seed(route_prefix='/api'))
        self.assertEqual([(c['file'], c['match']) for c in result['callers']], [('front/a.ts', 'loose')])

    def test_caller_built_from_constant_is_found_at_file_level(self) -> None:
        self.spring('@GetMapping("/{id}/history")\nX a() {}')
        self.write('front/b.ts', "const BASE = '/api/conversations';\n"
                                 "get(id) {\n  return http.get(`${BASE}/${id}` + '/history');\n}\n")
        result = surface.build(self.root, Seed(route_prefix='/api'))
        self.assertEqual([(c['file'], c['match'], c['line']) for c in result['callers']], [('front/b.ts', 'file', 3)])

    def test_test_callers_are_flagged(self) -> None:
        self.spring('@GetMapping("/{id}/history")\nX a() {}')
        self.write('front/a.spec.ts', "http.expectOne(`/api/conversations/${id}/history`);\n")
        result = surface.build(self.root, Seed(route_prefix='/api'))
        self.assertEqual([c['test'] for c in result['callers']], [True])

    def test_route_without_static_segment_is_unknown(self) -> None:
        self.spring('@GetMapping("/{id}")\nX a() {}', prefix='')
        result = surface.build(self.root, Seed(route_prefix='/'))
        self.assertTrue(any('no static segment' in u['reason'] for u in result['unknown']))

    def test_prefix_seed_does_not_select_sibling_prefix(self) -> None:
        self.spring('@GetMapping("/a")\nX a() {}')
        self.write('back/Other.java', '@RestController\n@RequestMapping("/api/conversations-archive")\n'
                                      'public class Other {\n@GetMapping("/b")\nX b() {}\n}\n')
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'))
        self.assertEqual([r['path'] for r in result['routes']], ['/api/conversations/a'])

    def test_path_seed(self) -> None:
        self.module()
        result = surface.build(self.root, Seed(paths=('back/src/main/java/app',)))
        self.assertEqual(len(result['routes']), 1)
        self.assertIn(SERVICE_IMPL, result['files'])

    def test_path_seed_outside_root_is_refused(self) -> None:
        with self.assertRaises(surface.SurfaceError):
            surface.build(self.root, Seed(paths=('../',)))

    def test_hops_stay_in_the_same_repository(self) -> None:
        self.module()
        for repo in ('back', 'other'):
            os.makedirs(os.path.join(self.root, repo, '.git'), exist_ok=True)
        self.write('other/ConversationService.ts', 'export class ConversationService {}\n')
        result = surface.build(self.root, Seed(route_prefix='/api/conversations'))
        self.assertNotIn('other/ConversationService.ts', result['files'])

    def test_output_paths_are_relative(self) -> None:
        self.module()
        result = surface.build(self.root, Seed(route_prefix='/api'))
        self.assertNotIn(self.root, json.dumps(result))

    def test_large_files_are_listed_in_limits(self) -> None:
        self.module()
        self.write('front/vendor.min.js', 'x' * (surface.MAX_FILE_BYTES + 1))
        result = surface.build(self.root, Seed(route_prefix='/api'))
        self.assertTrue(any('front/vendor.min.js' in limit for limit in result['limits']))


def git(cwd: str, *args: str) -> str:
    return subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, check=True).stdout


class GitSurface(SurfaceCase):
    def repo(self) -> str:
        back = os.path.join(self.root, 'back')
        os.makedirs(back)
        git(back, 'init', '-q', '-b', 'main')
        git(back, 'config', 'user.email', 'test@example.com')
        git(back, 'config', 'user.name', 'test')
        self.spring('private final ConversationService service;\n@PostMapping("/{id}/pin")\nX pin() {}')
        self.write(SERVICE_IFACE, 'public interface ConversationService { X pin(); }\n')
        self.write(SERVICE_IMPL, 'public class ConversationServiceImpl implements ConversationService {}\n')
        git(back, 'add', '.')
        git(back, 'commit', '-q', '-m', 'base')
        git(back, 'switch', '-q', '-c', 'feature')
        self.write(SERVICE_IMPL, 'public class ConversationServiceImpl implements ConversationService {\n'
                                 '@Transactional\npublic X pin() { return repository.save(x); }\n}\n')
        git(back, 'commit', '-q', '-am', 'pin writes')
        return back

    def test_diff_seed_reaches_the_route_above_a_changed_service(self) -> None:
        self.repo()
        result = surface.build(self.root, Seed(diff_base='main'))
        self.assertEqual([r['path'] for r in result['routes']], ['/api/conversations/{id}/pin'])
        self.assertIn(SERVICE_IMPL, result['files'])
        self.assertIn(JAVA_CTRL, result['files'])

    def test_commits_touching_module_files(self) -> None:
        self.repo()
        result = surface.build(self.root, Seed(route_prefix='/api'))
        subjects = [c['subject'] for c in result['commits']]
        self.assertEqual(subjects, ['pin writes', 'base'])
        self.assertIn(SERVICE_IMPL, result['commits'][0]['files'])

    def test_unknown_base_ref_is_reported(self) -> None:
        self.repo()
        result = surface.build(self.root, Seed(diff_base='no-such-ref'))
        self.assertTrue(any('base no-such-ref not found' in u['reason'] for u in result['unknown']))

    def test_ignored_and_outside_files_are_not_read(self) -> None:
        back = self.repo()
        self.write('back/.gitignore', 'generated/\n')
        self.write('back/generated/Copy.java', '@RestController\n@RequestMapping("/gen")\nclass Copy {\n'
                                               '@GetMapping("/a")\nX a() {}\n}\n')
        self.write('scratch/probe.ts', "fetch('/api/conversations/1/pin');\n")
        result = surface.build(self.root, Seed(route_prefix='/'))
        self.assertNotIn('/gen/a', [r['path'] for r in result['routes']])
        self.assertNotIn('scratch/probe.ts', [c['file'] for c in result['callers']])
        self.assertTrue(any('outside every git repository' in limit for limit in result['limits']))
        self.assertTrue(os.path.isdir(back))

    def test_option_like_base_ref_is_refused(self) -> None:
        self.repo()
        with self.assertRaises(surface.SurfaceError):
            surface.build(self.root, Seed(diff_base='--output=/tmp/x'))


class Cli(SurfaceCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = surface.main(['--root', self.root, *args])
        return code, out.getvalue()

    def test_seed_matching_nothing_exits_3(self) -> None:
        self.spring('@GetMapping("/a")\nX a() {}')
        code, out = self.run_main('--route-prefix', '/nothing')
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)['routes'], [])

    def test_found_surface_exits_0(self) -> None:
        self.spring('@GetMapping("/a")\nX a() {}')
        code, out = self.run_main('--route-prefix', '/api')
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)['routes']), 1)

    def test_invalid_seed_exits_2(self) -> None:
        code, _ = self.run_main('--path', '/etc')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
