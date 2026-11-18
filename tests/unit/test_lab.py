"""
Unit tests for the local validation lab (lab/run.py) that need no Docker:
target parsing, how the project's compose configuration becomes the lab, the
isolation checks that must refuse every unsafe variant, and the report.

The real run is covered by tests/e2e/test_lab_integration.py, opt-in with
PLAYBOOK_LAB_IT=1 (it downloads the scanner image).
"""
import copy
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('lab_run', os.path.join(_ROOT, 'lab', 'run.py'))
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)

# The shape `docker compose config --format json` resolves for a small project.
BASE = {
    'name': 'shop',
    'services': {
        'app': {
            'build': {'context': '/work/shop', 'dockerfile': 'Dockerfile'},
            'image': 'shop-app:dev',
            'container_name': 'shop-app',
            'ports': [{'mode': 'ingress', 'target': 8080, 'published': '8080', 'protocol': 'tcp'}],
            'networks': {'default': None},
            'depends_on': {'postgres': {'condition': 'service_healthy', 'required': True}},
        },
        'postgres': {
            'image': 'postgres:16',
            'ports': [{'mode': 'ingress', 'target': 5432, 'published': '5432', 'protocol': 'tcp'}],
            'networks': {'default': None},
            'volumes': [{'type': 'volume', 'source': 'pgdata', 'target': '/var/lib/postgresql/data'}],
        },
    },
    'networks': {'default': {'name': 'shop_default', 'ipam': {}}},
    'volumes': {'pgdata': {'name': 'shop_pgdata'}},
}


class _LabCase(unittest.TestCase):

    def setUp(self):
        self.report_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.report_dir)
        self.target = lab.parse_target('app', 8080, '/')

    def build(self, base=None):
        return lab.build_lab_config(copy.deepcopy(base or BASE), self.target,
                                    'redlab-shop', self.report_dir, 2)

    def problems(self, config, project_root='/work/shop'):
        return lab.validate_lab_config(config, 'app', self.report_dir, project_root)


class TestTarget(unittest.TestCase):

    def test_url_is_built_from_service_port_and_path(self):
        self.assertEqual(lab.parse_target('app', 8080, '/api').url, 'http://app:8080/api')

    def test_anything_that_could_name_another_host_is_refused(self):
        for path in ('//evil.example/', '/@evil.example', 'http://evil.example', 'api',
                     '/\\evil.example', '/a b'):
            with self.subTest(path=path), self.assertRaises(lab.LabError):
                lab.parse_target('app', 8080, path)

    def test_service_and_port_are_validated(self):
        for service, port in (('app;rm', 80), ('-app', 80), ('app', 0), ('app', 70000)):
            with self.subTest(service=service, port=port), self.assertRaises(lab.LabError):
                lab.parse_target(service, port, '/')


class TestBuildLabConfig(_LabCase):

    def test_the_built_lab_passes_validation(self):
        self.assertEqual(self.problems(self.build()), [])

    def test_nothing_reuses_the_developer_stack(self):
        config = self.build()
        self.assertEqual(config['name'], 'redlab-shop')
        for service in config['services'].values():
            self.assertNotIn('ports', service)
            self.assertNotIn('container_name', service)
        self.assertNotIn('image', config['services']['app'])  # built under the lab's name
        self.assertNotIn('name', config['networks']['default'])
        self.assertNotIn('name', config['volumes']['pgdata'])

    def test_scanner_and_target_share_only_the_internal_network(self):
        config = self.build()
        self.assertEqual(config['networks'][lab.NETWORK], {'internal': True})
        self.assertEqual(set(config['services'][lab.SCANNER]['networks']), {lab.NETWORK})
        self.assertIn(lab.NETWORK, config['services']['app']['networks'])
        self.assertIn('default', config['services']['app']['networks'])
        self.assertNotIn(lab.NETWORK, config['services']['postgres']['networks'])

    def test_scanner_command_targets_the_built_url(self):
        command = self.build()['services'][lab.SCANNER]['command']
        self.assertEqual(command[:3], ['zap-baseline.py', '-t', 'http://app:8080/'])

    def test_active_mode_runs_the_full_scan_with_a_capped_active_scan(self):
        config = lab.build_lab_config(copy.deepcopy(BASE), self.target, 'redlab-shop',
                                      self.report_dir, 2, active=True, active_minutes=7)
        command = config['services'][lab.SCANNER]['command']
        self.assertEqual(command[0], 'zap-full-scan.py')
        self.assertIn('http://app:8080/', command)
        z = command[command.index('-z') + 1]
        self.assertIn('scanner.maxScanDurationInMins=7', z)
        self.assertIn(f'scanner.maxRuleDurationInMins={lab.ACTIVE_RULE_MINUTES}', z)
        # Active changes the scan, never the isolation.
        self.assertEqual(self.problems(config), [])
        self.assertEqual(config['services'][lab.SCANNER]['cap_drop'], ['ALL'])
        self.assertEqual(set(config['services'][lab.SCANNER]['networks']), {lab.NETWORK})

    def test_passive_is_the_default(self):
        self.assertEqual(self.build()['services'][lab.SCANNER]['command'][0], 'zap-baseline.py')

    def test_the_active_timeout_covers_the_capped_active_scan_on_top(self):
        passive = lab.scanner_timeout_seconds(2)
        active = lab.scanner_timeout_seconds(2, active=True, active_minutes=10)
        self.assertEqual(active - passive, 10 * 60)
        self.assertGreater(active, (2 * (2 + lab.SCAN_OVERHEAD_MINUTES) + 2 + 10) * 60)

    def test_unknown_service_is_refused(self):
        target = lab.parse_target('api', 80, '/')
        with self.assertRaises(lab.LabError):
            lab.build_lab_config(copy.deepcopy(BASE), target, 'p', self.report_dir, 2)

    def test_target_without_explicit_networks_keeps_the_default_one(self):
        base = copy.deepcopy(BASE)
        del base['services']['app']['networks']
        self.assertIn('default', self.build(base)['services']['app']['networks'])

    def test_every_network_of_the_copied_stack_is_internal(self):
        """Ultra review of 4.0.0: only the scanner's network was internal, so
        the target could reach a database on this computer
        (host.docker.internal) or a staging host through its default network."""
        base = copy.deepcopy(BASE)
        base['services']['app']['extra_hosts'] = ['host.docker.internal=host-gateway']
        base['services']['app']['environment'] = {
            'DATABASE_URL': 'postgres://host.docker.internal:5432/dev'}
        base['services']['worker'] = {'image': 'shop-worker', 'networks': {'jobs': None}}
        base['networks']['jobs'] = None
        config = self.build(base)
        for name, network in config['networks'].items():
            with self.subTest(network=name):
                self.assertIs(network['internal'], True)
        self.assertEqual(self.problems(config), [])

    def test_a_service_without_networks_is_put_on_the_internal_default(self):
        base = copy.deepcopy(BASE)
        del base['services']['postgres']['networks']
        del base['networks']
        config = self.build(base)
        self.assertEqual(config['services']['postgres']['networks'], {'default': None})
        self.assertIs(config['networks']['default']['internal'], True)

    def test_fixed_subnets_addresses_and_bridge_names_are_dropped(self):
        """Ultra review of 4.0.0: a pinned subnet or bridge name made Docker
        refuse the lab's network while the developer's stack was up."""
        base = copy.deepcopy(BASE)
        base['networks']['default']['ipam'] = {'config': [{'subnet': '172.28.0.0/16'}]}
        base['networks']['default']['driver_opts'] = {_BRIDGE: 'br-shop', 'com.docker.network.driver.mtu': '1400'}
        base['services']['app']['networks'] = {'default': {'ipv4_address': '172.28.0.10',
                                                           'ipv6_address': 'fd00::10', 'aliases': ['web']}}
        config = self.build(base)
        self.assertNotIn('ipam', config['networks']['default'])
        self.assertEqual(config['networks']['default']['driver_opts'],
                         {'com.docker.network.driver.mtu': '1400'})
        self.assertEqual(config['services']['app']['networks']['default'], {'aliases': ['web']})
        self.assertEqual(self.problems(config), [])


_BRIDGE = 'com.docker.network.bridge.name'


class TestSqlmap(_LabCase):

    def sqlmap_target(self):
        return lab.parse_target('app', 8080, '/', endpoint='/items?id=1')

    def build_sqlmap(self):
        return lab.build_lab_config(copy.deepcopy(BASE), self.sqlmap_target(), 'redlab-shop',
                                    self.report_dir, 2, tool='sqlmap')

    def test_endpoint_with_a_query_is_accepted_and_built_into_the_url(self):
        t = self.sqlmap_target()
        self.assertEqual(t.query, 'id=1')
        self.assertEqual(t.url, 'http://app:8080/items?id=1')

    def test_an_endpoint_that_names_another_host_is_refused(self):
        for bad in ('/items?next=http://evil', '//evil/items?id=1', '/items?x=1@evil',
                    '/items?u=//evil'):
            with self.subTest(endpoint=bad), self.assertRaises(lab.LabError):
                lab.parse_target('app', 8080, '/', endpoint=bad)

    def test_sqlmap_service_is_the_built_image_with_a_bounded_command(self):
        config = self.build_sqlmap()
        scanner = config['services'][lab.SCANNER]
        self.assertEqual(scanner['image'], lab.SQLMAP_IMAGE)
        # -m (bulk) not -u: that is what makes sqlmap write the results CSV. The
        # endpoint URL travels in the targets file, never on the command line.
        self.assertEqual(scanner['command'][:2], ['sqlmap', '-m'])
        self.assertIn(f'{lab.REPORT_MOUNT}/{lab.SQLMAP_TARGETS_FILE}', scanner['command'])
        self.assertNotIn('http://app:8080/items?id=1', scanner['command'])
        self.assertIn('--batch', scanner['command'])
        self.assertIn('--level', scanner['command'])

    def test_the_sqlmap_scanner_is_as_isolated_as_the_zap_one(self):
        config = self.build_sqlmap()
        self.assertEqual(lab.validate_lab_config(config, 'app', self.report_dir, '/work/shop',
                                                 lab.SQLMAP_IMAGE), [])
        scanner = config['services'][lab.SCANNER]
        self.assertEqual(scanner['cap_drop'], ['ALL'])
        self.assertEqual(set(scanner['networks']), {lab.NETWORK})
        self.assertEqual(scanner['security_opt'], ['no-new-privileges:true'])

    def test_a_tampered_sqlmap_image_is_refused(self):
        config = self.build_sqlmap()
        config['services'][lab.SCANNER]['image'] = 'sqlmap:evil'
        problems = lab.validate_lab_config(config, 'app', self.report_dir, '/work/shop',
                                           lab.SQLMAP_IMAGE)
        self.assertTrue(any('pinned' in p for p in problems), problems)

    def test_results_csv_lists_injectable_parameters(self):
        with open(os.path.join(self.report_dir, lab.SQLMAP_RESULTS_FILE), 'w', newline='') as fh:
            fh.write('Target URL,Place,Parameter,Technique(s),Note(s)\n')
            fh.write('http://app:8080/items?id=1,GET,id,BEUST,\n')
        found = lab.parse_sqlmap_results(self.report_dir)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['parameter'], 'id')
        text = lab.format_sqlmap_findings(found, self.sqlmap_target())
        self.assertIn("SQL injection in GET parameter 'id'", text)
        self.assertIn('CWE-89', text)

    def test_no_results_file_means_not_injectable(self):
        self.assertEqual(lab.parse_sqlmap_results(self.report_dir), [])
        text = lab.format_sqlmap_findings([], self.sqlmap_target())
        self.assertIn('not injectable', text)

    def _run_sqlmap_scan(self, scanner_run):
        """Drive scan()'s sqlmap branch with Docker mocked. `scanner_run` gets
        the report dir, may write the results CSV, and returns the scanner run's
        CompletedProcess. scan() returns an exit code or raises LabError."""
        made: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(**kwargs: str) -> str:
            made.append(real_mkdtemp(**kwargs))
            return made[-1]

        def compose(_project, _config, *args, timeout=900, capture=True):
            if 'down' in args:
                return mock.Mock(returncode=0, stdout='', stderr='')
            if 'config' in args:
                return mock.Mock(returncode=0, stdout='{}', stderr='')
            if 'up' in args:
                return mock.Mock(returncode=0, stdout='', stderr='')
            return scanner_run(os.path.join(made[-1], 'report'))

        args = lab.argparse.Namespace(
            service='app', port=8080, path='/', minutes=1, startup_seconds=0,
            project_dir='.', compose_file=None,  active=False,
            active_minutes=lab.ACTIVE_SCAN_MINUTES, sqlmap=True, endpoint='/items?id=1',
            level=lab.SQLMAP_LEVEL, risk=lab.SQLMAP_RISK, time_sec=lab.SQLMAP_TIME_SEC)
        with mock.patch.object(lab, 'check_docker'), \
                mock.patch.object(lab, 'image_present', return_value=True), \
                mock.patch.object(lab, 'resolved_config', return_value={}), \
                mock.patch.object(lab, 'build_lab_config', return_value={}), \
                mock.patch.object(lab, 'validate_lab_config', return_value=[]), \
                mock.patch.object(lab, '_compose', side_effect=compose), \
                mock.patch.object(lab.tempfile, 'mkdtemp', side_effect=mkdtemp):
            return lab.scan(args)

    def test_a_run_that_cannot_reach_the_target_is_an_error_not_clean(self):
        def run(_report_dir):  # no CSV; sqlmap still exits 0 on a connection failure
            return mock.Mock(returncode=0, stdout='[CRITICAL] ' + lab.SQLMAP_UNREACHABLE,
                             stderr='')
        with self.assertRaises(lab.LabError) as ctx:
            self._run_sqlmap_scan(run)
        self.assertEqual(ctx.exception.code, lab.EXIT_ERROR)

    def test_a_run_that_writes_no_results_file_is_an_error(self):
        def run(_report_dir):
            return mock.Mock(returncode=0, stdout='sqlmap finished', stderr='')
        with self.assertRaises(lab.LabError) as ctx:
            self._run_sqlmap_scan(run)
        self.assertEqual(ctx.exception.code, lab.EXIT_ERROR)

    def test_a_results_file_with_an_injection_is_a_finding(self):
        def run(report_dir):
            with open(os.path.join(report_dir, lab.SQLMAP_RESULTS_FILE), 'w', newline='') as fh:
                fh.write('Target URL,Place,Parameter,Technique(s),Note(s)\n')
                fh.write('http://app:8080/items?id=1,GET,id,BEUST,\n')
            return mock.Mock(returncode=0, stdout='identified the following injection',
                             stderr='')
        self.assertEqual(self._run_sqlmap_scan(run), lab.EXIT_FINDINGS)

    def test_a_results_file_with_only_a_header_is_clean(self):
        def run(report_dir):
            with open(os.path.join(report_dir, lab.SQLMAP_RESULTS_FILE), 'w', newline='') as fh:
                fh.write('Target URL,Place,Parameter,Technique(s),Note(s)\n')
            return mock.Mock(returncode=0, stdout='all tested parameters do not appear',
                             stderr='')
        self.assertEqual(self._run_sqlmap_scan(run), lab.EXIT_CLEAN)


class TestValidation(_LabCase):

    def assertRefused(self, config, fragment):
        problems = self.problems(config)
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_network_not_internal(self):
        config = self.build()
        config['networks'][lab.NETWORK]['internal'] = False
        self.assertRefused(config, 'not internal')

    def test_scanner_on_another_network(self):
        config = self.build()
        config['services'][lab.SCANNER]['networks']['default'] = None
        self.assertRefused(config, 'only')

    def test_scanner_host_network_privileged_capabilities(self):
        for key, value in (('network_mode', 'host'), ('privileged', True),
                           ('cap_add', ['NET_ADMIN']), ('ports', ['8080:8080'])):
            config = self.build()
            config['services'][lab.SCANNER][key] = value
            with self.subTest(key=key):
                self.assertRefused(config, key)

    def test_scanner_extra_mount_or_other_image(self):
        config = self.build()
        config['services'][lab.SCANNER]['volumes'].append(
            {'type': 'bind', 'source': '/var/run/docker.sock', 'target': '/var/run/docker.sock'})
        self.assertRefused(config, 'Docker socket')
        config = self.build()
        config['services'][lab.SCANNER]['image'] = 'ghcr.io/zaproxy/zaproxy:stable'
        self.assertRefused(config, 'pinned')

    def test_third_service_on_the_lab_network(self):
        config = self.build()
        config['services']['postgres']['networks'][lab.NETWORK] = None
        self.assertRefused(config, 'only the scanner and the target')

    def test_external_network_or_volume(self):
        config = self.build()
        config['networks']['shared'] = {'external': True}
        self.assertRefused(config, 'external')
        config = self.build()
        config['volumes']['pgdata']['external'] = True
        self.assertRefused(config, 'external')

    def test_service_on_the_host_network(self):
        config = self.build()
        config['services']['postgres']['network_mode'] = 'host'
        self.assertRefused(config, 'network_mode')

    def test_a_writable_bind_is_dropped_so_the_lab_never_writes_to_the_host(self):
        """A read-write bind would write to the developer's files. build drops
        it (the image's own content stands in), so the config that starts holds
        no such mount and validate passes."""
        base = copy.deepcopy(BASE)
        base['services']['postgres']['volumes'] = [
            {'type': 'bind', 'source': '/work/shop/data', 'target': '/var/lib/postgresql/data'}]
        config = self.build(base)
        self.assertEqual(lab.host_mounts(config), [])
        self.assertEqual(self.problems(config), [])

    def test_a_writable_bind_reaching_validation_is_refused(self):
        """build should have dropped it; if an unresolved form reaches validate,
        it is refused rather than run."""
        config = self.build()
        config['services']['postgres']['volumes'] = [
            {'type': 'bind', 'source': '/work/shop/data', 'target': '/var/lib/postgresql/data'}]
        self.assertRefused(config, 'is writable')

    def test_a_read_only_bind_inside_the_project_is_kept_and_allowed(self):
        base = copy.deepcopy(BASE)
        base['services']['postgres']['volumes'] = [
            {'type': 'bind', 'source': '/work/shop/init', 'target': '/docker-entrypoint-initdb.d',
             'read_only': True}]
        config = self.build(base)
        self.assertEqual([m.target for m in lab.host_mounts(config)],
                         ['/docker-entrypoint-initdb.d'])
        self.assertEqual(self.problems(config), [])

    def test_a_read_only_bind_outside_the_project_is_refused(self):
        base = copy.deepcopy(BASE)
        base['services']['postgres']['volumes'] = [
            {'type': 'bind', 'source': '/etc/ssl', 'target': '/etc/ssl', 'read_only': True}]
        config = self.build(base)
        self.assertRefused(config, '/etc/ssl')
        self.assertTrue(any('outside the project' in p for p in self.problems(config)))

    def test_a_sibling_path_is_not_inside_the_project(self):
        """`/work/shop-data` shares a prefix with `/work/shop` but is not under
        it; the separator check must not treat it as inside."""
        base = copy.deepcopy(BASE)
        base['services']['postgres']['volumes'] = [
            {'type': 'bind', 'source': '/work/shop-data', 'target': '/data', 'read_only': True}]
        config = self.build(base)
        self.assertRefused(config, '/work/shop-data')

    def test_a_writable_host_bound_volume_becomes_a_fresh_volume(self):
        # `docker compose config` keeps it as `type: volume`; only driver_opts shows the host path.
        base = copy.deepcopy(BASE)
        base['volumes']['pgdata']['driver_opts'] = {
            'type': 'none', 'o': 'bind', 'device': '/home/dev/pgdata'}
        config = self.build(base)
        self.assertNotIn('driver_opts', config['volumes']['pgdata'])
        self.assertEqual(lab.host_mounts(config), [])
        self.assertEqual(self.problems(config), [])

    def test_a_read_only_host_bound_volume_inside_the_project_is_kept(self):
        base = copy.deepcopy(BASE)
        base['volumes']['pgdata']['driver_opts'] = {
            'type': 'none', 'o': 'bind', 'device': '/work/shop/seed'}
        base['services']['postgres']['volumes'][0]['read_only'] = True
        config = self.build(base)
        self.assertEqual(config['volumes']['pgdata']['driver_opts']['device'], '/work/shop/seed')
        self.assertEqual(self.problems(config), [])
        # Same mount, project root elsewhere: now outside the project, refused.
        self.assertTrue(any('/work/shop/seed' in p for p in self.problems(config, project_root='/other')))

    def test_volumes_of_a_container_outside_the_lab_are_refused(self):
        config = self.build()
        config['services']['postgres']['volumes_from'] = ['container:shop-postgres']
        self.assertRefused(config, 'shop-postgres')
        config = self.build()
        config['services']['postgres']['volumes_from'] = ['app']  # a lab service: fine
        self.assertEqual(self.problems(config), [])

    def test_the_reports_own_mount_is_not_a_project_bind_mount(self):
        self.assertEqual(self.problems(self.build()), [])

    def test_a_project_network_that_is_not_internal(self):
        config = self.build()
        config['networks']['default']['internal'] = False
        self.assertRefused(config, "network 'default' is not internal")

    def test_a_network_driver_that_reaches_a_real_network(self):
        for driver in ('macvlan', 'ipvlan', 'overlay', 'host'):
            config = self.build()
            config['networks']['default']['driver'] = driver
            with self.subTest(driver=driver):
                self.assertRefused(config, driver)
        config = self.build()
        config['networks']['default']['driver'] = 'bridge'
        self.assertEqual(self.problems(config), [])

    def test_a_fixed_subnet_address_or_bridge_name(self):
        config = self.build()
        config['networks']['default']['ipam'] = {'config': [{'subnet': '172.28.0.0/16'}]}
        self.assertRefused(config, 'fixed subnet')
        config = self.build()
        config['networks']['default']['driver_opts'] = {_BRIDGE: 'br-shop'}
        self.assertRefused(config, 'bridge name')
        config = self.build()
        config['services']['postgres']['networks']['default'] = {'ipv4_address': '172.28.0.5'}
        self.assertRefused(config, 'ipv4_address')

    # Ultra review of 4.0.0: these were checked on the scanner only, so any
    # project service copied into the lab could still reach the host. One
    # test per field that does (CLAUDE.md: enumerate every field of the format).

    def assertServiceRefused(self, key, value, fragment=None):
        config = self.build()
        config['services']['postgres'][key] = value
        self.assertRefused(config, fragment or key)

    def assertServiceAllowed(self, key, value):
        config = self.build()
        config['services']['postgres'][key] = value
        self.assertEqual(self.problems(config), [])

    def test_service_privileged(self):
        self.assertServiceRefused('privileged', True)

    def test_service_capabilities(self):
        for caps in (['SYS_ADMIN'], ['cap_sys_ptrace'], ['ALL'], ['NET_ADMIN']):
            with self.subTest(caps=caps):
                self.assertServiceRefused('cap_add', caps, 'capabilities')
        self.assertServiceAllowed('cap_add', ['IPC_LOCK', 'CAP_NET_BIND_SERVICE'])

    def test_service_devices(self):
        self.assertServiceRefused('devices', ['/dev/kvm:/dev/kvm'])

    def test_service_device_cgroup_rules(self):
        self.assertServiceRefused('device_cgroup_rules', ['c 1:3 mr'])

    def test_service_gpus(self):
        self.assertServiceRefused('gpus', 'all')

    def test_service_reserved_devices(self):
        self.assertServiceRefused(
            'deploy', {'resources': {'reservations': {'devices': [{'capabilities': ['gpu']}]}}},
            'reserves devices')

    def test_service_host_or_foreign_namespaces(self):
        for key in ('pid', 'ipc', 'uts', 'userns_mode', 'cgroup'):
            with self.subTest(key=key, value='host'):
                self.assertServiceRefused(key, 'host')
        for key in ('pid', 'ipc'):
            with self.subTest(key=key, value='container:'):
                self.assertServiceRefused(key, 'container:shop-app', key)
            with self.subTest(key=key, value='service:'):
                self.assertServiceAllowed(key, 'service:app')  # a lab service

    def test_service_network_mode(self):
        for mode in ('host', 'bridge', 'container:shop-app', 'service:app'):
            with self.subTest(mode=mode):
                self.assertServiceRefused('network_mode', mode)
        config = self.build()
        config['services']['postgres']['network_mode'] = 'none'
        del config['services']['postgres']['networks']
        self.assertEqual(self.problems(config), [])

    def test_service_external_links(self):
        self.assertServiceRefused('external_links', ['shop-redis:redis'], 'external_links')

    def test_service_docker_socket(self):
        self.assertServiceRefused(
            'volumes', [{'type': 'bind', 'source': '/var/run/docker.sock',
                         'target': '/var/run/docker.sock'}], 'Docker socket')

    def test_config_or_secret_from_a_host_file_outside_the_project(self):
        """A `configs:`/`secrets:` entry sourced from `file:` mounts a file of
        this computer; outside the project it is refused, as a bind mount is."""
        for kind in ('configs', 'secrets'):
            config = self.build()
            config[kind] = {'app_conf': {'file': '/etc/app.conf'}}
            with self.subTest(kind=kind):
                self.assertRefused(config, '/etc/app.conf')

    def test_config_or_secret_from_a_host_file_inside_the_project_is_allowed(self):
        for kind in ('configs', 'secrets'):
            config = self.build()
            config[kind] = {'app_conf': {'file': '/work/shop/app.conf'}}
            with self.subTest(kind=kind):
                self.assertEqual(self.problems(config), [])

    def test_external_config_or_secret(self):
        for kind in ('configs', 'secrets'):
            config = self.build()
            config[kind] = {'app_conf': {'external': True}}
            with self.subTest(kind=kind):
                self.assertRefused(config, 'is external')

    def test_a_config_or_secret_without_a_host_source_is_allowed(self):
        """An env-var-sourced config carries no file of this computer."""
        config = self.build()
        config['configs'] = {'app_conf': {'environment': 'APP_CONF'}}
        self.assertEqual(self.problems(config), [])

    def test_service_extra_hosts_host_gateway(self):
        """`host.docker.internal:host-gateway` reaches the host; compose may
        render extra_hosts as a mapping or as a list of `host:addr`."""
        self.assertServiceRefused(
            'extra_hosts', {'host.docker.internal': 'host-gateway'}, 'host-gateway')
        self.assertServiceRefused(
            'extra_hosts', ['host.docker.internal:host-gateway'], 'host-gateway')

    def test_service_extra_hosts_to_a_fixed_address_is_allowed(self):
        """A pinned address is not routable on the lab's internal network, and
        is not predictable since the lab strips fixed subnets."""
        self.assertServiceAllowed('extra_hosts', {'api.internal': '10.5.0.9'})

    # The lab keeps `build:` and builds on the host daemon, so a build that
    # reaches files of this computer is a host reach too. One test per field.

    def test_service_build_forwards_ssh(self):
        self.assertServiceRefused('build', {'context': '/work/shop', 'ssh': ['default']}, 'build.ssh')

    def test_service_build_context_outside_the_project(self):
        self.assertServiceRefused('build', {'context': '/home/dev/other'},
                                  'context outside the project')

    def test_service_build_context_inside_the_project_is_allowed(self):
        self.assertServiceAllowed('build', {'context': '/work/shop/api'})

    def test_service_build_additional_context_outside_the_project(self):
        for extra in ({'seed': '/home/dev/private'}, ['seed=/home/dev/private']):
            with self.subTest(form=type(extra).__name__):
                self.assertServiceRefused('build',
                                          {'context': '/work/shop', 'additional_contexts': extra},
                                          'extra context outside')

    def test_service_build_additional_context_inside_or_an_image_is_allowed(self):
        self.assertServiceAllowed('build', {'context': '/work/shop', 'additional_contexts': {
            'seed': '/work/shop/seed', 'base': 'docker-image://alpine:3.20'}})

    def test_a_host_volume_used_rw_anywhere_is_freshened_for_every_user(self):
        """`freshen` is keyed by volume name, so a host-bound volume used
        read-write by one service loses its host binding for the read-only
        user too: that consumer gets a fresh empty volume, not the seed data.
        Fails closed (no host write); documented here."""
        base = copy.deepcopy(BASE)
        base['volumes']['shared'] = {
            'driver_opts': {'type': 'none', 'o': 'bind', 'device': '/work/shop/shared'}}
        base['services']['app']['volumes'] = [{'type': 'volume', 'source': 'shared', 'target': '/a'}]
        base['services']['postgres']['volumes'].append(
            {'type': 'volume', 'source': 'shared', 'target': '/b', 'read_only': True})
        config = self.build(base)
        self.assertNotIn('driver_opts', config['volumes']['shared'])
        self.assertEqual(self.problems(config), [])


class TestReport(unittest.TestCase):

    REPORT = {'site': [{'@name': 'http://app:8080', 'alerts': [
        {'name': 'Content Security Policy (CSP) Header Not Set', 'riskcode': '2',
         'cweid': '693', 'instances': [{'uri': 'http://app:8080/', 'evidence': ''}]},
        {'name': 'Cross Site Scripting (Reflected)', 'riskcode': '3', 'cweid': '79',
         'instances': [{'uri': 'http://app:8080/hello?name=x', 'evidence': '<script>'}]},
        {'name': 'Server Leaks Version', 'riskcode': '1', 'cweid': '497', 'instances': []},
    ]}]}

    def test_alerts_sorted_by_risk_with_their_fiche(self):
        alerts = lab.parse_report(self.REPORT)
        self.assertEqual([a['risk'] for a in alerts], [3, 2, 1])
        self.assertEqual(alerts[0]['fiche'], 'xss.md')
        self.assertIn('engineering-security', alerts[1]['fiche'])

    def test_summary_counts_medium_and_high(self):
        text = lab.format_findings(lab.parse_report(self.REPORT), lab.parse_target('app', 8080, '/'))
        self.assertIn('[High] Cross Site Scripting (Reflected) (CWE-79', text)
        self.assertIn('2 Medium/High finding(s)', text)

    def test_empty_report(self):
        text = lab.format_findings([], lab.parse_target('app', 8080, '/'))
        self.assertIn('no alerts', text)

    def test_the_header_names_the_mode(self):
        target = lab.parse_target('app', 8080, '/')
        self.assertIn('passive', lab.format_findings([], target))
        self.assertIn('active', lab.format_findings([], target, active=True))


class TestEnvironment(unittest.TestCase):

    def _completed(self, code=0, out='', err=''):
        return mock.Mock(returncode=code, stdout=out, stderr=err)

    def test_old_compose_is_refused(self):
        with mock.patch.object(lab, '_run', side_effect=[self._completed(out='27.0.1'),
                                                         self._completed(out='2.17.3')]):
            with self.assertRaises(lab.LabError) as ctx:
                lab.check_docker()
        self.assertIn('too old', str(ctx.exception))

    def test_daemon_permission_points_to_the_docker_group(self):
        err = 'permission denied while trying to connect to the Docker daemon socket'
        with mock.patch.object(lab, '_run', return_value=self._completed(1, err=err)):
            with self.assertRaises(lab.LabError) as ctx:
                lab.check_docker()
        self.assertIn('cannot reach the daemon', str(ctx.exception))

    def test_scan_without_the_image_points_to_setup(self):
        with mock.patch.object(lab, 'check_docker'), \
                mock.patch.object(lab, 'image_present', return_value=False):
            code = lab.main(['--service', 'app', '--port', '8080'])
        self.assertEqual(code, lab.EXIT_REFUSED)

    def test_a_failed_teardown_still_removes_the_resolved_config(self):
        """Release review of 4.0.0: when `down` failed, compose.json (resolved
        environment values) stayed in the work dir."""
        made: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(**kwargs: str) -> str:
            made.append(real_mkdtemp(**kwargs))
            return made[-1]

        def compose(_project: str, _config: str, *args: str, timeout: float = 900) -> mock.Mock:
            if 'down' in args:
                raise OSError('down failed')
            if 'config' in args:
                return self._completed(out='{}')
            return self._completed(1, err='up failed')

        args = lab.argparse.Namespace(service='app', port=8080, path='/', minutes=1,
                                      startup_seconds=0, project_dir='.', compose_file=None,
                                       active=False,
                                      active_minutes=lab.ACTIVE_SCAN_MINUTES, sqlmap=False,
                                      endpoint=None, level=lab.SQLMAP_LEVEL, risk=lab.SQLMAP_RISK,
                                      time_sec=lab.SQLMAP_TIME_SEC)
        with mock.patch.object(lab, 'check_docker'), \
                mock.patch.object(lab, 'image_present', return_value=True), \
                mock.patch.object(lab, 'resolved_config', return_value={}), \
                mock.patch.object(lab, 'build_lab_config', return_value={}), \
                mock.patch.object(lab, 'validate_lab_config', return_value=[]), \
                mock.patch.object(lab, '_compose', side_effect=compose), \
                mock.patch.object(lab.tempfile, 'mkdtemp', side_effect=mkdtemp):
            with self.assertRaises(OSError):
                lab.scan(args)
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))

    def _scan_with(self, compose: object) -> list[tuple[str, ...]]:
        """Run `scan` with Docker replaced by `compose`; the compose verbs it
        was called with, in order. A LabError from the scan is swallowed: the
        tests read what ran, and what did not."""
        calls: list[tuple[str, ...]] = []

        def recording(project: str, config: str, *args: str, timeout: float = 900) -> object:
            calls.append(args)
            return compose(project, config, *args, timeout=timeout)

        args = lab.argparse.Namespace(service='app', port=8080, path='/', minutes=1,
                                      startup_seconds=0, project_dir='.', compose_file=None,
                                       active=False,
                                      active_minutes=lab.ACTIVE_SCAN_MINUTES, sqlmap=False,
                                      endpoint=None, level=lab.SQLMAP_LEVEL, risk=lab.SQLMAP_RISK,
                                      time_sec=lab.SQLMAP_TIME_SEC)
        with mock.patch.object(lab, 'check_docker'), \
                mock.patch.object(lab, 'image_present', return_value=True), \
                mock.patch.object(lab, 'resolved_config', return_value={}), \
                mock.patch.object(lab, 'build_lab_config', return_value={}), \
                mock.patch.object(lab, 'validate_lab_config', return_value=[]), \
                mock.patch.object(lab, '_compose', side_effect=recording), \
                mock.patch.object(lab.sys, 'stderr', io.StringIO()):
            try:
                lab.scan(args)
            except lab.LabError:
                pass
        return calls

    def test_a_leftover_of_a_killed_run_is_removed_before_the_lab_starts(self):
        """A run killed before its teardown (SIGKILL) left its containers,
        volumes and images behind, and nothing ever removed them."""
        def compose(_p: str, _c: str, *args: str, timeout: float = 900) -> mock.Mock:
            if 'config' in args:
                return self._completed(out='{}')
            return self._completed(0 if 'down' in args else 1, err='up failed')
        verbs = [next(a for a in call if a in ('config', 'down', 'up')) for call in self._scan_with(compose)]
        self.assertEqual(verbs, ['config', 'down', 'up', 'down'])

    def test_a_leftover_that_cannot_be_removed_starts_nothing(self):
        def compose(_p: str, _c: str, *args: str, timeout: float = 900) -> mock.Mock:
            if 'config' in args:
                return self._completed(out='{}')
            return self._completed(1, err='network in use')
        calls = self._scan_with(compose)
        self.assertFalse(any('up' in call for call in calls), calls)

    def test_a_second_run_of_the_same_project_is_refused_while_one_runs(self):
        """Two sessions share the project's lab name; without the lock the
        second would tear down the first one's lab as a leftover."""
        held = lab.acquire_lab_lock(lab._project_name('.'))
        try:
            def compose(*_args: str, timeout: float = 900) -> mock.Mock:
                raise AssertionError('Docker must not be called while another run holds the lab')
            with mock.patch.object(lab, 'check_docker'), \
                    mock.patch.object(lab, 'image_present', return_value=True), \
                    mock.patch.object(lab, 'resolved_config', return_value={}), \
                    mock.patch.object(lab, '_compose', side_effect=compose), \
                    mock.patch.object(lab.sys, 'stderr', io.StringIO()) as err:
                code = lab.main(['--service', 'app', '--port', '8080'])
            self.assertEqual(code, lab.EXIT_REFUSED)
            self.assertIn('in progress', err.getvalue())
        finally:
            os.close(held)

    def test_the_lock_is_released_when_a_run_ends(self):
        def compose(_p: str, _c: str, *args: str, timeout: float = 900) -> mock.Mock:
            return self._completed(out='{}') if 'config' in args else self._completed(1)
        self._scan_with(compose)
        os.close(lab.acquire_lab_lock(lab._project_name('.')))  # raises if still held

    def _private_tmp(self) -> str:
        """A temporary directory the lab sees as the system one for this test."""
        private = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, private, True)
        patcher = mock.patch.object(lab.tempfile, 'tempdir', private)
        patcher.start()
        self.addCleanup(patcher.stop)
        return private

    def test_a_killed_runs_work_dir_is_removed_before_the_next_run(self):
        """A run killed before its `finally` kept its work dir, and with it
        compose.json and the environment values resolved into it. Another
        project whose name extends this one, and a link, are left alone."""
        private = self._private_tmp()
        project = lab._project_name('.')
        stale = os.path.join(private, f'{project}.killed')
        other = os.path.join(private, f'{project}-v2.running')
        elsewhere = os.path.join(private, 'elsewhere')
        for path in (stale, other, elsewhere):
            os.mkdir(path)
            with open(os.path.join(path, 'compose.json'), 'w', encoding='utf-8') as fh:
                fh.write('{}')
        os.symlink(elsewhere, os.path.join(private, f'{project}.link'))

        def compose(_p: str, _c: str, *args: str, timeout: float = 900) -> mock.Mock:
            return self._completed(out='{}') if 'config' in args else self._completed(1)
        self._scan_with(compose)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(other, 'compose.json')))
        self.assertTrue(os.path.exists(os.path.join(elsewhere, 'compose.json')))
        # The run's own work dir went too: only the lock file is new.
        self.assertEqual(set(os.listdir(private)),
                         {os.path.basename(other), 'elsewhere', f'{project}.link',
                          f'{project}-{os.getuid()}.lock'})

    def test_the_lock_does_not_follow_a_link_planted_at_its_path(self):
        private = self._private_tmp()
        project = lab._project_name('.')
        victim = os.path.join(private, 'victim')
        open(victim, 'w', encoding='utf-8').close()
        os.symlink(victim, os.path.join(private, f'{project}-{os.getuid()}.lock'))
        with self.assertRaises(lab.LabError):
            lab.acquire_lab_lock(project)

    def test_a_lock_file_another_user_created_is_refused(self):
        self._private_tmp()
        with mock.patch.object(lab.os, 'fstat',
                               return_value=mock.Mock(st_uid=os.getuid() + 1)), \
                self.assertRaises(lab.LabError) as ctx:
            lab.acquire_lab_lock(lab._project_name('.'))
        self.assertIn('another user', str(ctx.exception))

    def test_without_fcntl_the_lab_points_to_wsl(self):
        """A top-level `import fcntl` failed on native Windows before any
        message could say where the lab runs."""
        with mock.patch.dict(lab.sys.modules, {'fcntl': None}), \
                self.assertRaises(lab.LabError) as ctx:
            lab.acquire_lab_lock('redlab-x')
        self.assertIn('WSL 2', str(ctx.exception))

    def test_the_outer_timeout_covers_every_phase_zap_bounds(self):
        """Ultra review of 4.0.0: zap-baseline applies -T to its start-up wait
        and to its passive-scan wait separately, with the spider (-m) between;
        the outer limit allowed only m+7 minutes and killed scans ZAP could
        still finish."""
        for minutes in (1, 2, 10):
            command = lab.build_lab_config(copy.deepcopy(BASE), lab.parse_target('app', 8080, '/'),
                                           'p', '/tmp/r', minutes)['services'][lab.SCANNER]['command']
            zap_limit = int(command[command.index('-T') + 1])
            spider = int(command[command.index('-m') + 1])
            with self.subTest(minutes=minutes):
                self.assertGreater(lab.scanner_timeout_seconds(minutes), (2 * zap_limit + spider) * 60)

    def _teardown_with(self, side_effect: object) -> tuple[object, str]:
        err = io.StringIO()
        with mock.patch.object(lab, '_compose', side_effect=side_effect), \
                mock.patch.object(lab.sys, 'stderr', err):
            try:
                result: object = lab.teardown('redlab-shop', '/tmp/compose.json')
            except KeyboardInterrupt as exc:
                result = exc
        return result, err.getvalue()

    def test_a_failed_teardown_is_reported_with_the_command_that_finishes_it(self):
        """Ultra review of 4.0.0: the result of `down` was discarded."""
        removed, err = self._teardown_with([self._completed(1, err='network in use')])
        self.assertFalse(removed)
        self.assertIn('network in use', err)
        self.assertIn('docker compose -p redlab-shop down -v', err)

    def test_a_teardown_that_times_out_is_reported(self):
        removed, err = self._teardown_with(lab.LabError('timed out', lab.EXIT_ERROR))
        self.assertFalse(removed)
        self.assertIn('docker compose -p redlab-shop down -v', err)

    def test_a_second_interrupt_during_teardown_is_not_reported_as_removed(self):
        result, err = self._teardown_with(KeyboardInterrupt())
        self.assertIsInstance(result, KeyboardInterrupt)
        self.assertIn('docker compose -p redlab-shop down -v', err)

    def test_a_successful_teardown(self):
        removed, err = self._teardown_with([self._completed(0)])
        self.assertTrue(removed)
        self.assertEqual(err, '')

    def test_an_interrupt_says_removed_only_after_a_successful_teardown(self):
        def run(down_code: int) -> str:
            def compose(_project: str, _config: str, *args: str, timeout: float = 900) -> mock.Mock:
                if 'down' in args:
                    return self._completed(down_code, err='boom')
                if 'config' in args:
                    return self._completed(out='{}')
                raise KeyboardInterrupt
            err = io.StringIO()
            with mock.patch.object(lab, 'check_docker'), \
                    mock.patch.object(lab, 'image_present', return_value=True), \
                    mock.patch.object(lab, 'resolved_config', return_value={}), \
                    mock.patch.object(lab, 'build_lab_config', return_value={}), \
                    mock.patch.object(lab, 'validate_lab_config', return_value=[]), \
                    mock.patch.object(lab, '_compose', side_effect=compose), \
                    mock.patch.object(lab.sys, 'stderr', err):
                code = lab.main(['--service', 'app', '--port', '8080', '--startup-seconds', '0'])
            self.assertEqual(code, lab.EXIT_ERROR)
            return err.getvalue()
        self.assertIn('the lab was removed', run(0))
        failed = run(1)
        self.assertNotIn('the lab was removed', failed)
        self.assertIn('finish it with', failed)

    def test_version_parsing(self):
        self.assertEqual(lab.parse_version('v2.29.7-desktop.1'), (2, 29, 7))
        self.assertEqual(lab.parse_version('garbage'), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
