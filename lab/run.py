#!/usr/bin/env python3
"""
Local validation lab: scan the project's own application, running on this
machine, with OWASP ZAP — passive by default, active (attack payloads) with
--active — against an isolated copy of its stack, and nothing else.

    python3 lab/run.py --setup                        # check Docker, download the scanner image
    python3 lab/run.py --service app --port 8080      # passive scan of the `app` service
    python3 lab/run.py --service app --port 8080 --active   # active scan (sends payloads)
    python3 lab/run.py --uninstall                    # remove the scanner image

Both modes run only against the isolated target on the internal network; the
isolation below is identical for each. Active only sends payloads to that
target, a throwaway copy, and never reaches anything outside the lab.

How the scope is enforced, outermost first:

1. The lab never touches the developer's running stack. It renders the
   project's compose configuration into its own file and runs it as a
   separate compose project, with every published port removed, every fixed
   container/network/volume name, subnet and address dropped, and its own
   fresh named volumes.
2. Every network of the lab is declared `internal`: no route to the internet,
   the local network or this computer. The scanner is attached to one of
   them, `redlab`, whose only other member is the target service. A published
   host, staging, production, a database running on this computer
   (`host.docker.internal`): unreachable by construction, for the scanner and
   for every service of the copied stack, whatever URL anyone types or the
   project's environment holds. No service may share a namespace, device or
   container with the host or with anything outside the lab.
3. The target is not a URL. It is a service of the compose file plus a port
   and a path, and the URL is built here.
4. Before anything starts, the rendered configuration — and the configuration
   Docker Compose resolves from it — are both checked against those rules.
   Any violation aborts with exit code 2.

Nothing is kept. The report is written to a temporary directory, printed,
and deleted; the lab's containers, network, volumes and locally built images
are removed when the run ends, also on error or Ctrl-C; a removal that fails
is reported with the command that finishes it. Only the scanner images (ZAP,
pulled; sqlmap, built from pinned source) stay in Docker's cache, so the next
run does not fetch them again; `--uninstall` removes them.

Exit codes: 0 no Medium/High finding, 1 findings, 2 refused (environment or
scope), 3 Docker or scanner error.

Python 3.10+, standard library only.
"""
import argparse
import csv
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

# OWASP ZAP, official image, pinned by digest (multi-arch index: amd64 and
# arm64). Moving to a newer ZAP is a change to this line, reviewed like code.
ZAP_IMAGE = ('ghcr.io/zaproxy/zaproxy@sha256:'
             '9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af')
ZAP_DOWNLOAD_HINT = 'about 1.2 GB to download, 3.8 GB on disk once extracted'
MIN_FREE_BYTES = 5 * 1024 ** 3

# The SQL-injection scanner, built locally from a pinned, official source
# (sqlmap on PyPI, published by the sqlmap project) by lab/sqlmap.Dockerfile.
# The base image it builds on is pinned by digest there. Deeper on SQLi than the
# ZAP active rule: it confirms, fingerprints the DBMS and characterises the flaw.
SQLMAP_VERSION = '1.10.9'
SQLMAP_IMAGE = f'redlab-sqlmap:{SQLMAP_VERSION}'
SQLMAP_DOCKERFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sqlmap.Dockerfile')
# Modest defaults: one endpoint, no crawl. The user raises them on a confirmed
# candidate. --time-sec bounds each time-based probe; the outer timeout the run.
SQLMAP_LEVEL, SQLMAP_RISK, SQLMAP_TIME_SEC = 1, 1, 5
SQLMAP_RESULTS_FILE = 'sqlmap.csv'
SQLMAP_TARGETS_FILE = 'targets.txt'
# sqlmap exits 0 whether or not it reached the target (1.10.9: a connection
# failure is logged and start() returns, the exit status is never set), so a
# failed scan is told from a clean one by this critical line in its output.
SQLMAP_UNREACHABLE = 'unable to connect to the target URL'
SQLMAP_RUN_MINUTES = 15  # outer cap on one sqlmap run against one endpoint
# ZAP's -T, in minutes beyond the spider budget. zap-baseline applies it twice,
# each phase on its own clock: once to wait for ZAP to start, once to wait for
# the passive scan of what the spider found. The spider (-m) runs in between.
SCAN_OVERHEAD_MINUTES = 5
# Beyond ZAP's own bounds: `compose run` start-up and writing the report.
SCANNER_MARGIN_SECONDS = 120
# Active scan (zap-full-scan) sends payloads and runs to completion on its own
# clock -- neither -m nor -T bounds it -- so the lab caps it with ZAP config:
# the whole active scan, and any single rule, stop at these minute budgets.
# The outer timeout covers this budget on top of the spider and the waits.
ACTIVE_SCAN_MINUTES = 10
ACTIVE_RULE_MINUTES = 2

# Capabilities a project service may add in the lab: Docker's default set
# (adding one is a no-op) and the few that act only inside the container.
# Anything else (SYS_ADMIN, SYS_PTRACE, NET_ADMIN, ALL...) is refused.
_ALLOWED_CAPS = frozenset({
    'AUDIT_WRITE', 'CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'FSETID', 'KILL', 'MKNOD',
    'NET_BIND_SERVICE', 'NET_RAW', 'SETFCAP', 'SETGID', 'SETPCAP', 'SETUID', 'SYS_CHROOT',
    'IPC_LOCK', 'SYS_NICE', 'SYS_RESOURCE',
})
# Namespace modes that join the host's namespace or another container's.
# `service:<name>` stays inside the lab and is allowed.
_NAMESPACE_KEYS = ('pid', 'ipc', 'uts', 'userns_mode', 'cgroup')
# Network drivers that stay on this computer. macvlan, ipvlan and overlay put
# the container on a real network.
_LOCAL_NETWORK_DRIVERS = ('', 'bridge')
# A fixed bridge name collides with the developer's running stack, as a fixed
# subnet does: Docker refuses a second network with either.
_BRIDGE_NAME_OPT = 'com.docker.network.bridge.name'

SCANNER = 'redlab-zap'
NETWORK = 'redlab'
REPORT_MOUNT = '/zap/wrk'
REPORT_FILE = 'report.json'
MIN_COMPOSE = (2, 20, 0)

EXIT_CLEAN, EXIT_FINDINGS, EXIT_REFUSED, EXIT_ERROR = 0, 1, 2, 3

_PATH_RE = re.compile(r'^/(?!/)[A-Za-z0-9._~!$&\'()*+,;=:%/-]*$')
# A query string (sqlmap needs one, e.g. `id=1`): the path charset plus `?` and
# `&`, still with no way to name another host.
_QUERY_RE = re.compile(r'^[A-Za-z0-9._~!$&\'()*+,;=:%/?&=-]*$')
_SERVICE_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')

# CWE of a ZAP alert -> where the playbook explains the fix.
CWE_FICHE = {
    79: 'xss.md', 80: 'xss.md', 89: 'injection.md', 78: 'injection.md', 94: 'injection.md',
    943: 'injection.md', 1336: 'injection.md', 601: 'redirects-and-headers.md',
    113: 'redirects-and-headers.md', 918: 'ssrf.md', 22: 'files.md', 434: 'files.md',
    611: 'untrusted-parsing.md', 502: 'untrusted-parsing.md', 639: 'access-control.md',
    284: 'access-control.md', 285: 'access-control.md', 352: 'tokens-sessions.md',
    614: 'tokens-sessions.md', 1004: 'tokens-sessions.md', 1275: 'tokens-sessions.md',
    327: 'crypto.md', 319: 'crypto.md', 326: 'crypto.md',
}
_SKILL_SECTION = 'engineering-security, "HTTP security"'
RISK_NAMES = {0: 'Informational', 1: 'Low', 2: 'Medium', 3: 'High'}


class LabError(Exception):
    """A run that cannot go on. `code` is the exit code."""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Target:
    service: str
    port: int
    path: str
    query: str = ''

    @property
    def url(self) -> str:
        suffix = f'?{self.query}' if self.query else ''
        return f'http://{self.service}:{self.port}{self.path}{suffix}'


# ─── Docker environment ──────────────────────────────────────────────────────

def _run(args: list[str], timeout: float = 60, cwd: str | None = None,
         capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, timeout=timeout, text=True,
                              capture_output=capture)
    except FileNotFoundError as exc:
        raise LabError(f'`{args[0]}` is not installed.\n' + install_hint()) from exc
    except subprocess.TimeoutExpired as exc:
        raise LabError(f'`{" ".join(args[:3])}` timed out after {timeout:.0f}s.', EXIT_ERROR) from exc


def install_hint() -> str:
    system = platform.system()
    if system == 'Linux':
        return ('Install Docker Engine and the Compose plugin for your distribution: '
                'https://docs.docker.com/engine/install/ — then let your user reach the '
                'daemon without sudo: `sudo usermod -aG docker "$USER"` and log in again.')
    if system in ('Darwin', 'Windows'):
        return ('Install Docker Desktop (includes Compose): https://docs.docker.com/desktop/ '
                '— on Windows, run the lab from WSL 2.')
    return 'Install Docker and the Compose plugin: https://docs.docker.com/get-docker/'


def parse_version(text: str) -> tuple[int, int, int]:
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', text or '')
    if not match:
        return (0, 0, 0)
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def check_docker() -> None:
    """Docker CLI present, daemon reachable by this user, Compose new enough."""
    server = _run(['docker', 'version', '--format', '{{.Server.Version}}'])
    if server.returncode != 0:
        detail = (server.stderr or '').strip().splitlines()[-1:] or ['']
        if 'permission denied' in (server.stderr or '').lower():
            raise LabError('Docker is installed but this user cannot reach the daemon '
                           f'({detail[0]}).\n' + install_hint())
        raise LabError(f'The Docker daemon is not running or not reachable ({detail[0]}). '
                       'Start Docker and try again.')
    compose = _run(['docker', 'compose', 'version', '--short'])
    if compose.returncode != 0:
        raise LabError('The Docker Compose plugin is missing.\n' + install_hint())
    if parse_version(compose.stdout) < MIN_COMPOSE:
        wanted = '.'.join(map(str, MIN_COMPOSE))
        raise LabError(f'Docker Compose {compose.stdout.strip()} is too old; the lab needs '
                       f'{wanted} or newer.\n' + install_hint())


def image_present(image: str = ZAP_IMAGE) -> bool:
    return _run(['docker', 'image', 'inspect', image]).returncode == 0


def build_sqlmap_image() -> None:
    """Build the SQLi scanner locally from the pinned Dockerfile (online: pip
    fetches sqlmap). Context is the Dockerfile's own directory, sent empty via
    stdin so nothing else is copied in."""
    print(f'Building the SQLi scanner from source (sqlmap {SQLMAP_VERSION}, pinned):\n'
          f'  {SQLMAP_IMAGE}', flush=True)
    built = _run(['docker', 'build', '-f', SQLMAP_DOCKERFILE,
                  '--build-arg', f'SQLMAP_VERSION={SQLMAP_VERSION}',
                  '-t', SQLMAP_IMAGE, os.path.dirname(SQLMAP_DOCKERFILE)],
                 timeout=1800, capture=False)
    if built.returncode != 0 or not image_present(SQLMAP_IMAGE):
        raise LabError('Building the SQLi scanner image failed (see the output above).', EXIT_ERROR)


def setup() -> int:
    check_docker()
    if image_present() and image_present(SQLMAP_IMAGE):
        print('Ready. Both scanner images are already in Docker\'s cache:\n'
              f'  {ZAP_IMAGE}\n  {SQLMAP_IMAGE}')
        return EXIT_CLEAN
    if not image_present():
        root = _run(['docker', 'info', '--format', '{{.DockerRootDir}}']).stdout.strip()
        try:
            free = shutil.disk_usage(root).free if root else None
        except OSError:
            free = None  # Docker Desktop keeps it inside a VM: not measurable from here
        if free is not None and free < MIN_FREE_BYTES:
            raise LabError(f'Not enough free disk space under {root}: '
                           f'{free / 1024 ** 3:.1f} GB free, the scanner image needs about '
                           f'{MIN_FREE_BYTES // 1024 ** 3} GB.')
        print(f'Downloading the OWASP ZAP image ({ZAP_DOWNLOAD_HINT}), pinned by digest:\n'
              f'  {ZAP_IMAGE}\nIt stays in Docker\'s cache until `lab/run.py --uninstall`.',
              flush=True)
        pulled = _run(['docker', 'pull', ZAP_IMAGE], timeout=3600, capture=False)
        if pulled.returncode != 0 or not image_present():
            raise LabError('Downloading the scanner image failed (see the output above).', EXIT_ERROR)
    if not image_present(SQLMAP_IMAGE):
        build_sqlmap_image()
    print('Ready.')
    return EXIT_CLEAN


def uninstall() -> int:
    check_docker()
    present = [img for img in (ZAP_IMAGE, SQLMAP_IMAGE) if image_present(img)]
    if not present:
        print('The scanner images are not installed; nothing to remove.')
        return EXIT_CLEAN
    removed = _run(['docker', 'image', 'rm', *present])
    if removed.returncode != 0:
        raise LabError(f'Could not remove the images: {removed.stderr.strip()}', EXIT_ERROR)
    print('Scanner images removed. `lab/run.py --setup` installs them again.')
    return EXIT_CLEAN


# ─── Configuration ───────────────────────────────────────────────────────────

def parse_target(service: str, port: int, path: str, endpoint: str | None = None) -> Target:
    if not _SERVICE_RE.match(service or ''):
        raise LabError(f'Invalid service name: {service!r}.')
    if not 1 <= port <= 65535:
        raise LabError(f'Invalid port: {port}.')
    # An endpoint (for sqlmap) is a path plus an optional ?query; a plain path
    # has no query. Either way it must name a path on the target, never a host.
    raw = endpoint if endpoint is not None else path
    path_part, _, query = raw.partition('?')
    if not _PATH_RE.match(path_part) or '@' in path_part or '\\' in path_part:
        raise LabError(f'Invalid path {path_part!r}: it must start with a single "/" and name a '
                       'path on the target service, not another host.')
    if query and (not _QUERY_RE.match(query) or '@' in query or '\\' in query or '//' in query):
        raise LabError(f'Invalid query {query!r}: it must be a query string on the target '
                       'service, not another host.')
    return Target(service, port, path_part, query)


def bind_mounts(config: dict) -> list[str]:
    """`service: host path -> container path` for every bind mount, including
    a named volume the local driver binds to a host path (`driver_opts.device`):
    Compose resolves it as `type: volume`, but it writes the same files."""
    host_volumes = {}
    for volume_name, volume in (config.get('volumes') or {}).items():
        device = (volume.get('driver_opts') or {}).get('device') if isinstance(volume, dict) else None
        if device:
            host_volumes[volume_name] = device
    found = []
    for name, service in (config.get('services') or {}).items():
        for volume in service.get('volumes') or []:
            if not isinstance(volume, dict):
                continue
            source, target = volume.get('source'), volume.get('target')
            if volume.get('type') == 'bind':
                found.append(f'{name}: {source} -> {target}')
            elif volume.get('type') == 'volume' and source in host_volumes:
                found.append(f'{name}: {host_volumes[source]} (volume {source!r}) -> {target}')
    return found


def scanner_command(url: str, minutes: int, active: bool, active_minutes: int) -> list[str]:
    """ZAP's command line. Passive (`zap-baseline.py`) crawls and reads; active
    (`zap-full-scan.py`) also sends attack payloads to the target. Both run only
    against the isolated target on the internal network: active changes the scan,
    never the isolation. The active scan is capped by ZAP config, since -m/-T do
    not bound it."""
    common = ['-t', url, '-J', REPORT_FILE, '-I',
              '-m', str(minutes), '-T', str(minutes + SCAN_OVERHEAD_MINUTES)]
    if not active:
        return ['zap-baseline.py', *common]
    return ['zap-full-scan.py', *common,
            '-z', f'-config scanner.maxScanDurationInMins={active_minutes} '
                  f'-config scanner.maxRuleDurationInMins={ACTIVE_RULE_MINUTES}']


def sqlmap_command(level: int, risk: int, time_sec: int) -> list[str]:
    """sqlmap against one endpoint of the isolated target, non-interactive and
    bounded. The endpoint is passed through a one-line targets file (-m), not
    -u: only bulk mode makes sqlmap write the --results-file CSV that the lab
    reads back (sqlmap 1.10.9 _setResultsFile returns early unless
    conf.multipleTargets, which -u alone never sets). The targets file, the CSV
    and sqlmap's output-dir all land under the mounted report dir; the CSV names
    each injectable parameter and the technique that proved it."""
    return ['sqlmap', '-m', f'{REPORT_MOUNT}/{SQLMAP_TARGETS_FILE}',
            '--batch', '--disable-coloring',
            '--level', str(level), '--risk', str(risk),
            '--time-sec', str(time_sec), '--flush-session',
            '--output-dir', REPORT_MOUNT,
            '--results-file', f'{REPORT_MOUNT}/{SQLMAP_RESULTS_FILE}']


def build_lab_config(base: dict, target: Target, project: str, report_dir: str,
                     minutes: int, active: bool = False,
                     active_minutes: int = ACTIVE_SCAN_MINUTES,
                     tool: str = 'zap', level: int = SQLMAP_LEVEL,
                     risk: int = SQLMAP_RISK, time_sec: int = SQLMAP_TIME_SEC) -> dict:
    """The project's resolved compose configuration, turned into the lab.

    Ports, fixed names, subnets, addresses and image tags of built services
    are dropped so that nothing collides with, or reuses, the developer's own
    stack; every network becomes internal, so no service reaches past the lab;
    the target joins the scanner's network; the scanner is added on it alone.
    `tool` picks the scanner (ZAP or sqlmap) and `active` the ZAP scan; neither
    loosens any of the above -- the scanner is the same locked-down service, on
    the internal network alone, mounting only the report dir, whichever it is."""
    config = json.loads(json.dumps(base))  # deep copy of plain JSON data
    config['name'] = project
    for item in (config.get('volumes') or {}).values():
        if isinstance(item, dict):
            item.pop('name', None)
    services = config.setdefault('services', {})
    if target.service not in services:
        known = ', '.join(sorted(services)) or 'none'
        raise LabError(f'Service {target.service!r} is not in the compose file '
                       f'(services: {known}).')
    for service in services.values():
        service.pop('ports', None)
        service.pop('container_name', None)
        if 'build' in service:
            service.pop('image', None)  # built under the lab's name, removed by `down --rmi local`
        attached = service.get('networks')
        if isinstance(attached, list):
            attached = {n: None for n in attached}
        if not attached and not service.get('network_mode'):
            attached = {'default': None}  # what Compose attaches it to anyway
        if attached:
            service['networks'] = attached
        for attachment in (attached or {}).values():
            if isinstance(attachment, dict):
                attachment.pop('ipv4_address', None)
                attachment.pop('ipv6_address', None)
    target_service = services[target.service]
    networks = target_service.get('networks') or {'default': None}
    networks[NETWORK] = None
    target_service['networks'] = networks
    lab_networks = config.get('networks')
    if not isinstance(lab_networks, dict):
        lab_networks = config['networks'] = {}
    for service in services.values():
        for name in service.get('networks') or {}:
            lab_networks.setdefault(name, {})
    for name, network in list(lab_networks.items()):
        if not isinstance(network, dict):
            lab_networks[name] = network = {}
        network.pop('name', None)
        network.pop('ipam', None)
        if isinstance(network.get('driver_opts'), dict):
            network['driver_opts'].pop(_BRIDGE_NAME_OPT, None)
        network['internal'] = True
    lab_networks[NETWORK] = {'internal': True}
    if tool == 'sqlmap':
        image, command = SQLMAP_IMAGE, sqlmap_command(level, risk, time_sec)
    else:
        # -m bounds the spider; -T bounds ZAP's start-up and, separately, the
        # passive scan; an active scan is capped by ZAP config in scanner_command.
        # The outer timeout (scanner_timeout_seconds) covers every phase.
        image, command = ZAP_IMAGE, scanner_command(target.url, minutes, active, active_minutes)
    services[SCANNER] = {
        'image': image,
        'networks': {NETWORK: None},
        'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'],
        'volumes': [{'type': 'bind', 'source': report_dir, 'target': REPORT_MOUNT}],
        'command': command,
        'profiles': [NETWORK],
    }
    return config


def scanner_timeout_seconds(minutes: int, active: bool = False,
                            active_minutes: int = ACTIVE_SCAN_MINUTES) -> int:
    """How long `compose run` of the scanner may take: ZAP's start-up wait and
    its passive-scan wait (each bounded by -T on its own), the spider (-m), and
    for an active scan its capped duration on top. A shorter limit killed a scan
    ZAP was still allowed to finish, and its report was lost."""
    zap_limit = minutes + SCAN_OVERHEAD_MINUTES
    total = 2 * zap_limit + minutes + (active_minutes if active else 0)
    return total * 60 + SCANNER_MARGIN_SECONDS


def _host_reach(name: str, service: dict) -> list[str]:
    """Every field of a compose service that shares something of this computer,
    or of a container outside the lab, with it."""
    problems = []
    if service.get('privileged'):
        problems.append(f'service {name!r} is privileged')
    caps = {str(c).upper().removeprefix('CAP_') for c in service.get('cap_add') or []}
    if caps - _ALLOWED_CAPS:
        problems.append(f'service {name!r} adds capabilities {", ".join(sorted(caps - _ALLOWED_CAPS))}')
    for key in ('devices', 'device_cgroup_rules', 'gpus'):
        if service.get(key):
            problems.append(f'service {name!r} uses {key!r} of this computer')
    reservations = ((service.get('deploy') or {}).get('resources') or {}).get('reservations') or {}
    if reservations.get('devices'):
        problems.append(f'service {name!r} reserves devices of this computer (deploy)')
    for key in _NAMESPACE_KEYS:
        value = str(service.get(key) or '')
        if value == 'host' or value.startswith('container:'):
            problems.append(f'service {name!r} sets {key}: {value!r}, outside the lab')
    network_mode = service.get('network_mode')
    if network_mode and network_mode != 'none':
        problems.append(f'service {name!r} uses network_mode {network_mode!r}')
    if service.get('external_links'):
        problems.append(f'service {name!r} links to containers outside the lab (external_links)')
    for source in service.get('volumes_from') or []:
        if str(source).startswith('container:'):
            problems.append(f'service {name!r} takes the volumes of container '
                            f'{str(source)[len("container:"):]!r}, which is outside the lab')
    for volume in service.get('volumes') or []:
        source = str(volume.get('source') if isinstance(volume, dict) else volume)
        if 'docker.sock' in source:
            problems.append(f'service {name!r} mounts the Docker socket')
    return problems


def validate_lab_config(config: dict, target_service: str, report_dir: str,
                        allow_bind_mounts: bool, scanner_image: str = ZAP_IMAGE) -> list[str]:
    """Every way `config` could let the scanner reach beyond the lab, or let the
    lab reach into the developer's environment. [] when it is safe to start.
    `scanner_image` is the pinned image the scanner must be (ZAP or sqlmap)."""
    problems: list[str] = []
    networks = config.get('networks') or {}
    services = config.get('services') or {}
    for name, network in networks.items():
        network = network if isinstance(network, dict) else {}
        if network.get('external'):
            problems.append(f'network {name!r} is external: the lab would join a network '
                            'it does not own')
        if network.get('internal') is not True:
            problems.append(f'network {name!r} is not internal: its services could reach '
                            'this computer, the local network or the internet')
        if (network.get('driver') or '') not in _LOCAL_NETWORK_DRIVERS:
            problems.append(f'network {name!r} uses the {network["driver"]!r} driver, which '
                            'reaches a real network')
    if NETWORK not in networks:
        problems.append(f'network {NETWORK!r} is missing')
    for name, volume in (config.get('volumes') or {}).items():
        if isinstance(volume, dict) and volume.get('external'):
            problems.append(f'volume {name!r} is external: the lab would reuse real data')

    scanner = services.get(SCANNER)
    if not scanner:
        problems.append('the scanner service is missing')
    else:
        if scanner.get('image') != scanner_image:
            problems.append('the scanner image is not the pinned one')
        if set((scanner.get('networks') or {}).keys()) != {NETWORK}:
            problems.append(f'the scanner must be on {NETWORK!r} only')
        # Stricter than any project service: the scanner adds no capability at all.
        for key in ('network_mode', 'cap_add', 'ports'):
            if scanner.get(key):
                problems.append(f'the scanner sets {key!r}')
        mounts = scanner.get('volumes') or []
        if (len(mounts) != 1 or not isinstance(mounts[0], dict)
                or os.path.realpath(str(mounts[0].get('source'))) != os.path.realpath(report_dir)
                or mounts[0].get('target') != REPORT_MOUNT):
            problems.append('the scanner may mount only the temporary report directory')

    target = services.get(target_service) or {}
    if NETWORK not in (target.get('networks') or {}):
        problems.append(f'the target service is not on {NETWORK!r}')
    members = [n for n, s in services.items() if NETWORK in (s.get('networks') or {})]
    if sorted(members) != sorted({SCANNER, target_service}):
        problems.append(f'only the scanner and the target may join {NETWORK!r} '
                        f'(found: {", ".join(sorted(members))})')

    for name, service in services.items():
        if service.get('ports'):
            problems.append(f'service {name!r} publishes ports on the host')
        if service.get('container_name'):
            problems.append(f'service {name!r} has a fixed container name')
        for network_name, attachment in (service.get('networks') or {}).items():
            for key in ('ipv4_address', 'ipv6_address'):
                if isinstance(attachment, dict) and attachment.get(key):
                    problems.append(f'service {name!r} has a fixed {key} on {network_name!r}')
        problems += _host_reach(name, service)
    for name, network in networks.items():
        if isinstance(network, dict) and ((network.get('ipam') or {}).get('config')
                                          or (network.get('driver_opts') or {}).get(_BRIDGE_NAME_OPT)):
            problems.append(f'network {name!r} has a fixed subnet or bridge name')
    if not allow_bind_mounts:
        found = [b for b in bind_mounts(config) if not b.startswith(f'{SCANNER}:')]
        if found:
            problems.append(
                'the project mounts folders of this computer into its containers, so the '
                'lab would read and write the same files as your development setup:\n    '
                + '\n    '.join(found)
                + '\n  Re-run with --allow-bind-mounts if that is acceptable for this scan.')
    return problems


# ─── Report ──────────────────────────────────────────────────────────────────

def parse_report(report: dict) -> list[dict]:
    """ZAP's JSON report as a flat list of alerts, highest risk first."""
    alerts = []
    for site in report.get('site') or []:
        for alert in site.get('alerts') or []:
            try:
                risk = int(alert.get('riskcode', 0))
                cwe = int(alert.get('cweid') or 0)
            except (TypeError, ValueError):
                risk, cwe = 0, 0
            instances = alert.get('instances') or []
            alerts.append({
                'risk': risk,
                'name': alert.get('name') or alert.get('alert') or '?',
                'cwe': cwe,
                'fiche': CWE_FICHE.get(cwe, _SKILL_SECTION if cwe else ''),
                'count': len(instances) or int(alert.get('count') or 0),
                'uri': (instances[0].get('uri') if instances else '') or '',
                'evidence': ((instances[0].get('evidence') if instances else '') or '')[:120],
            })
    return sorted(alerts, key=lambda a: (-a['risk'], a['name']))


def format_findings(alerts: list[dict], target: Target, active: bool = False) -> str:
    serious = [a for a in alerts if a['risk'] >= 2]
    mode = 'OWASP ZAP full scan, active' if active else 'OWASP ZAP baseline, passive'
    lines = [f'Lab scan of {target.url} ({mode}):']
    if not alerts:
        return '\n'.join(lines + ['  no alerts.'])
    for alert in alerts:
        cwe = f'CWE-{alert["cwe"]}' if alert['cwe'] else 'no CWE'
        where = f' at {alert["uri"]}' if alert['uri'] else ''
        lines.append(f'- [{RISK_NAMES.get(alert["risk"], alert["risk"])}] {alert["name"]} '
                     f'({cwe}, {alert["count"]} instance(s)){where}')
        if alert['evidence']:
            lines.append(f'    evidence: {alert["evidence"]}')
        if alert['fiche']:
            lines.append(f'    fix: {alert["fiche"]}')
    lines.append('')
    lines.append(f'{len(serious)} Medium/High finding(s). Each confirmed finding gets the '
                 'negative test from its fiche (engineering-testing) before it is fixed.'
                 if serious else 'No Medium/High findings.')
    return '\n'.join(lines)


def parse_sqlmap_results(report_dir: str) -> list[dict]:
    """sqlmap's results CSV as a list of injectable parameters. Empty when the
    endpoint is not injectable (no CSV, or only a header)."""
    path = os.path.join(report_dir, SQLMAP_RESULTS_FILE)
    if not os.path.exists(path):
        return []
    found = []
    with open(path, newline='', encoding='utf-8', errors='replace') as fh:
        for row in csv.DictReader(fh):
            place = (row.get('Place') or '').strip()
            parameter = (row.get('Parameter') or '').strip()
            if parameter:
                found.append({'place': place, 'parameter': parameter,
                              'technique': (row.get('Technique(s)') or '').strip()})
    return found


def format_sqlmap_findings(found: list[dict], target: Target) -> str:
    lines = [f'Lab SQL-injection scan of {target.url} (sqlmap {SQLMAP_VERSION}):']
    if not found:
        return '\n'.join(lines + ['  not injectable on the parameters tested.'])
    for hit in found:
        lines.append(f'- [High] SQL injection in {hit["place"]} parameter '
                     f'{hit["parameter"]!r} (CWE-89, technique {hit["technique"] or "?"})')
    lines += ['', f'{len(found)} injectable parameter(s). Confirmed SQL injection: write the '
              'negative test from injection.md (engineering-testing), then fix the query.']
    return '\n'.join(lines)


# ─── Scan ────────────────────────────────────────────────────────────────────

def _compose(project: str, config_file: str, *args: str, timeout: float = 900,
             capture: bool = True) -> subprocess.CompletedProcess:
    return _run(['docker', 'compose', '-p', project, '-f', config_file, *args],
                timeout=timeout, capture=capture)


def _project_name(project_dir: str) -> str:
    base = re.sub(r'[^a-z0-9_-]', '-', os.path.basename(os.path.abspath(project_dir)).lower())
    return f'redlab-{base.strip("-") or "project"}'


def resolved_config(project_dir: str, compose_files: list[str]) -> dict:
    args = ['docker', 'compose']
    for file in compose_files:
        args += ['-f', file]
    done = _run(args + ['config', '--format', 'json'], cwd=project_dir, timeout=120)
    if done.returncode != 0:
        raise LabError('Docker Compose could not read the project configuration:\n'
                       + (done.stderr or '').strip())
    return json.loads(done.stdout)


def scan(args: argparse.Namespace) -> int:
    tool = 'sqlmap' if args.sqlmap else 'zap'
    if args.sqlmap and args.active:
        raise LabError('--active is a ZAP option; --sqlmap is a separate SQL-injection scan.')
    if tool == 'sqlmap':
        if not args.endpoint:
            raise LabError('--sqlmap needs --endpoint, e.g. --endpoint "/items?id=1".')
        if not 1 <= args.level <= 5 or not 1 <= args.risk <= 3 or args.time_sec < 1:
            raise LabError('--level must be 1-5, --risk 1-3, --time-sec 1 or more.')
        target = parse_target(args.service, args.port, args.path, endpoint=args.endpoint)
    else:
        target = parse_target(args.service, args.port, args.path)
    if args.minutes < 1 or args.startup_seconds < 0:
        raise LabError('--minutes must be 1 or more and --startup-seconds 0 or more.')
    if args.active_minutes < 1:
        raise LabError('--active-minutes must be 1 or more.')
    check_docker()
    needed = SQLMAP_IMAGE if tool == 'sqlmap' else ZAP_IMAGE
    if not image_present(needed):
        raise LabError('The scanner image is not installed. Run `lab/run.py --setup` first'
                       + (f' ({ZAP_DOWNLOAD_HINT}).' if tool == 'zap' else '.'))
    base = resolved_config(args.project_dir, args.compose_file)
    project = _project_name(args.project_dir)

    workdir = tempfile.mkdtemp(prefix='redlab-')  # 0700: only this user can enter
    report_dir = os.path.join(workdir, 'report')
    os.mkdir(report_dir)
    # The scanner runs as its own user in the container; it reaches this
    # directory only through the mount, never through the 0700 parent.
    os.chmod(report_dir, 0o777)  # noqa: S103
    config_file = os.path.join(workdir, 'compose.json')
    started = interrupted = False
    try:
        config = build_lab_config(base, target, project, report_dir, args.minutes,
                                  active=args.active, active_minutes=args.active_minutes,
                                  tool=tool, level=args.level, risk=args.risk,
                                  time_sec=args.time_sec)
        problems = validate_lab_config(config, target.service, report_dir,
                                       args.allow_bind_mounts, needed)
        with open(config_file, 'w', encoding='utf-8') as fh:
            json.dump(config, fh)
        os.chmod(config_file, 0o600)  # resolved environment values live in it
        if not problems:
            effective = _compose(project, config_file, '--profile', NETWORK,
                                 'config', '--format', 'json', timeout=120)
            if effective.returncode != 0:
                raise LabError('Docker Compose rejected the lab configuration:\n'
                               + effective.stderr.strip(), EXIT_ERROR)
            problems = validate_lab_config(json.loads(effective.stdout), target.service,
                                           report_dir, args.allow_bind_mounts, needed)
        if problems:
            raise LabError('Refused — the lab would not be isolated:\n  - '
                           + '\n  - '.join(problems))

        print(f'Starting an isolated copy of the stack (compose project {project!r})...',
              flush=True)
        started = True
        up = _compose(project, config_file, 'up', '-d', '--wait', target.service)
        if up.returncode != 0:
            raise LabError('The target service did not start:\n' + up.stderr.strip()[-2000:],
                           EXIT_ERROR)
        if args.startup_seconds:
            time.sleep(args.startup_seconds)
        if tool == 'sqlmap':
            # The endpoint reaches sqlmap through a one-line targets file in the
            # mounted report dir, not on the command line: only -m makes sqlmap
            # write the results CSV that is read back below (see sqlmap_command).
            with open(os.path.join(report_dir, SQLMAP_TARGETS_FILE), 'w',
                      encoding='utf-8') as fh:
                fh.write(target.url + '\n')
            print(f'Running a SQL-injection scan of {target.url}...', flush=True)
            done = _compose(project, config_file, '--profile', NETWORK, 'run', '--rm',
                            '--no-deps', SCANNER,
                            timeout=SQLMAP_RUN_MINUTES * 60 + SCANNER_MARGIN_SECONDS)
            # sqlmap exits 0 even when it never reached the target, so without
            # this a failed scan would be reported as "not injectable". Tell a
            # transport failure and a scan that never ran from a real clean
            # result by the critical line and by the results file existing.
            if SQLMAP_UNREACHABLE in (done.stdout or '') + (done.stderr or ''):
                raise LabError('sqlmap could not reach the target on that port and path; '
                               'nothing was tested.', EXIT_ERROR)
            if not os.path.exists(os.path.join(report_dir, SQLMAP_RESULTS_FILE)):
                raise LabError('sqlmap produced no results file; the scan did not complete.',
                               EXIT_ERROR)
            found = parse_sqlmap_results(report_dir)
            print(format_sqlmap_findings(found, target))
            return EXIT_FINDINGS if found else EXIT_CLEAN
        kind = 'active scan' if args.active else 'passive scan'
        print(f'Running a {kind} of {target.url}...', flush=True)
        _compose(project, config_file, '--profile', NETWORK, 'run', '--rm', '--no-deps',
                 SCANNER, timeout=scanner_timeout_seconds(args.minutes, args.active,
                                                          args.active_minutes))
        report_path = os.path.join(report_dir, REPORT_FILE)
        if not os.path.exists(report_path):
            raise LabError('The scanner produced no report (the target may not have '
                           'answered on that port and path).', EXIT_ERROR)
        with open(report_path, encoding='utf-8') as fh:
            alerts = parse_report(json.load(fh))
        print(format_findings(alerts, target, args.active))
        return EXIT_FINDINGS if any(a['risk'] >= 2 for a in alerts) else EXIT_CLEAN
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        # The work dir holds compose.json with the resolved environment values,
        # so it goes even when `down` fails: the failure still reaches the user.
        try:
            if started and teardown(project, config_file) and interrupted:
                print('[lab] the lab was removed.', file=sys.stderr)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def teardown(project: str, config_file: str) -> bool:
    """Remove the lab. True once `down` succeeded; on failure, or on an
    interrupt while it runs (a second Ctrl-C), say so with the command that
    finishes the job, so a leftover copy of the stack is never reported gone."""
    manual = f'docker compose -p {project} down -v --remove-orphans'
    try:
        done = _compose(project, config_file, '--profile', NETWORK, 'down', '-v',
                        '--rmi', 'local', '--remove-orphans', timeout=300)
    except KeyboardInterrupt:
        print(f'[lab] interrupted while removing the lab; finish it with: {manual}',
              file=sys.stderr)
        raise
    except LabError as exc:
        print(f'[lab] could not remove the lab ({exc}); finish it with: {manual}',
              file=sys.stderr)
        return False
    if done.returncode != 0:
        detail = (done.stderr or '').strip()[-1000:]
        print(f'[lab] could not remove the lab:\n{detail}\n[lab] finish it with: {manual}',
              file=sys.stderr)
        return False
    return True


def _terminate(signum: int, _frame: object) -> None:
    # SIGTERM (a `timeout`, a closed terminal, a CI cancel) would otherwise end
    # the process without running `finally`, leaving the lab's containers up.
    raise KeyboardInterrupt(f'signal {signum}')


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _terminate)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, _terminate)
    parser = argparse.ArgumentParser(
        description='Scan your own application, running locally in an isolated copy of its '
                    'compose stack, with OWASP ZAP (passive by default, --active for attack '
                    'payloads). Nothing outside the lab can be targeted, and nothing is kept '
                    'after the run.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--setup', action='store_true',
                      help='check Docker and download the pinned scanner image')
    mode.add_argument('--uninstall', action='store_true', help='remove the scanner image')
    parser.add_argument('--service', help='compose service to scan')
    parser.add_argument('--port', type=int, help='port the service listens on inside the network')
    parser.add_argument('--path', default='/', help='path to start from (default: /)')
    parser.add_argument('--project-dir', default='.', help='directory of the compose project')
    parser.add_argument('--compose-file', action='append', default=[],
                        help='compose file(s), as with `docker compose -f` (default: Compose\'s own)')
    parser.add_argument('--minutes', type=int, default=2, help='spider time budget (default: 2)')
    parser.add_argument('--startup-seconds', type=int, default=5,
                        help='extra wait after the service reports started (default: 5)')
    parser.add_argument('--allow-bind-mounts', action='store_true',
                        help='scan even though the project mounts folders of this computer')
    parser.add_argument('--active', action='store_true',
                        help='send attack payloads (ZAP full scan), not only a passive read; '
                             'slower, and still only against the isolated target')
    parser.add_argument('--active-minutes', type=int, default=ACTIVE_SCAN_MINUTES,
                        help=f'cap on the active scan (default: {ACTIVE_SCAN_MINUTES})')
    parser.add_argument('--sqlmap', action='store_true',
                        help='deep SQL-injection scan of one --endpoint with sqlmap, instead '
                             'of ZAP; same isolation')
    parser.add_argument('--endpoint',
                        help='for --sqlmap: path with the parameter to test, e.g. "/items?id=1"')
    parser.add_argument('--level', type=int, default=SQLMAP_LEVEL,
                        help=f'sqlmap level 1-5 (default: {SQLMAP_LEVEL})')
    parser.add_argument('--risk', type=int, default=SQLMAP_RISK,
                        help=f'sqlmap risk 1-3 (default: {SQLMAP_RISK})')
    parser.add_argument('--time-sec', type=int, default=SQLMAP_TIME_SEC, dest='time_sec',
                        help=f'sqlmap time-based delay seconds (default: {SQLMAP_TIME_SEC})')
    args = parser.parse_args(argv)
    try:
        if args.setup:
            return setup()
        if args.uninstall:
            return uninstall()
        if not args.service or not args.port:
            parser.error('--service and --port are required to scan (or use --setup)')
        return scan(args)
    except LabError as exc:
        print(f'[lab] {exc}', file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print('[lab] interrupted.', file=sys.stderr)
        return EXIT_ERROR


if __name__ == '__main__':
    sys.exit(main())
