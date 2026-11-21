"""
What a template test puts in package.json for each package, resolved at run
time to match what a new project installs ("Dependency versions" in
engineering-governance).

- The registry's `latest` tag, when it names a stable release. It goes in as
  the tag, not as its version: npm resolves a tag the way a bare `npm install
  <name>` does, and steps down to a release that satisfies the peers of the
  other packages (TypeScript 7 is `latest` while @nestjs/swagger accepts only
  5 or 6). An exact version would fail with ERESOLVE instead.
- The newest stable release, when the tag names a prerelease: the prisma CLI
  once tagged a Prisma 8 release candidate as `latest` while @prisma/client
  stayed on 7.
- For coupled packages (a CLI, its client and their adapters) and their
  anchor, the anchor's release as an exact version, so they cannot drift apart.
"""
import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping

_STABLE_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')
_NPM_VIEW_TIMEOUT_S = 120
_LATEST = 'latest'


def _key(version: str) -> tuple[int, int, int]:
    match = _STABLE_RE.match(version)
    if not match:
        raise ValueError(f'not a stable version: {version}')
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def pick_release(dist_tags: Mapping[str, str], versions: Iterable[str]) -> str:
    latest = dist_tags.get(_LATEST, '')
    if _STABLE_RE.match(latest):
        return latest
    stable = [v for v in versions if _STABLE_RE.match(v)]
    if not stable:
        raise ValueError(f'no stable release (latest tag: {latest or "none"})')
    return max(stable, key=_key)


def install_spec(dist_tags: Mapping[str, str], versions: Iterable[str]) -> str:
    if _STABLE_RE.match(dist_tags.get(_LATEST, '')):
        return _LATEST
    return pick_release(dist_tags, versions)


def _view(name: str) -> tuple[dict[str, str], list[str]]:
    done = subprocess.run(['npm', 'view', name, 'dist-tags', 'versions', '--json'], capture_output=True,
                          text=True, timeout=_NPM_VIEW_TIMEOUT_S, check=False)
    if done.returncode != 0:
        raise RuntimeError(f'npm view {name} failed: {done.stderr.strip()}')
    meta = json.loads(done.stdout)
    if isinstance(meta, list) and len(meta) == 1:
        meta = meta[0]  # npm 11 wraps a multi-field view in a one-item list; older npm does not
    if not isinstance(meta, dict):
        raise RuntimeError(f'npm view {name}: unexpected output shape {type(meta).__name__}')
    versions = meta.get('versions', [])
    return meta.get('dist-tags', {}), [versions] if isinstance(versions, str) else versions


def _resolved(name: str, pick: Callable[[Mapping[str, str], Iterable[str]], str]) -> str:
    try:
        return pick(*_view(name))
    except ValueError as error:
        raise RuntimeError(f'{name}: {error}') from error


def resolve(names: Iterable[str], coupled: Mapping[str, str] | None = None) -> dict[str, str]:
    """The package.json spec of each of `names`. `coupled` maps a package to the
    anchor whose release it takes; the anchor must be one of `names`."""
    coupled = coupled or {}
    wanted = list(names)
    anchors = set(coupled.values())
    missing = sorted(anchors - set(wanted))
    if missing:
        raise ValueError(f'coupling anchors not in the package list: {missing}')
    specs = {name: _resolved(name, pick_release if name in anchors else install_spec)
             for name in wanted if name not in coupled}
    specs.update({name: specs[anchor] for name, anchor in coupled.items() if name in wanted})
    return specs


def package_json(name: str, dependencies: Iterable[str], dev_dependencies: Iterable[str],
                 coupled: Mapping[str, str] | None = None, **fields: object) -> dict[str, object]:
    runtime, development = tuple(dependencies), tuple(dev_dependencies)
    specs = resolve((*runtime, *development), coupled)
    return {
        'name': name,
        'private': True,
        **fields,
        'dependencies': {dep: specs[dep] for dep in runtime},
        'devDependencies': {dep: specs[dep] for dep in development},
    }
