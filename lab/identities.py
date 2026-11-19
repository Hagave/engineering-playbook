"""
Identity mode of the lab, host side: the personas file, the route catalog read
from the code, and the report of what the identity client found inside the lab.

The catalog comes from what the application declares (audit/surface.py reads
the route declarations), completed by its OpenAPI document when it serves one.
No REST convention is assumed: a route is the method and path the code
declares, whatever that method does.
"""
from __future__ import annotations

import json
import os
import re
import sys

_LAB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_LAB), 'audit'))
sys.path.insert(0, _LAB)
import surface
from identity_client import path_shape

ALLOWED_ROLES = frozenset({'customer'})
LOGIN_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'GET'})
_PATH_RE = re.compile(r'^/(?!/)[A-Za-z0-9._~!$&\'()*+,;=:%/-]*$')
_HEADER_RE = re.compile(r'^[A-Za-z0-9-]+$')
_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,40}$')
# Headers a persona may not set: they would redirect the request or change how
# the lab itself talks to the target.
_RESERVED_HEADERS = frozenset({'host', 'content-length', 'transfer-encoding', 'connection'})
OPENAPI_CANDIDATES = ('/openapi.json', '/api-json', '/api/docs-json', '/docs-json',
                      '/swagger.json', '/v3/api-docs', '/api/openapi.json', '/api/swagger.json')
REDACTED = '***'


class IdentityError(ValueError):
    """The personas file cannot be used."""


def _within(path: str, root: str) -> bool:
    real_root, real = os.path.realpath(root), os.path.realpath(path)
    return real == real_root or real.startswith(real_root + os.sep)


def _check_persona(index: int, persona: object) -> list[str]:
    where = f'persona #{index + 1}'
    if not isinstance(persona, dict):
        return [f'{where} is not an object']
    problems = []
    name = persona.get('name')
    if not isinstance(name, str) or not _NAME_RE.match(name):
        problems.append(f'{where}: "name" must be 1-40 letters, digits, "_", "." or "-"')
    else:
        where = f'persona {name!r}'
    if persona.get('role') not in ALLOWED_ROLES:
        problems.append(f'{where}: "role" must be "customer"; the lab signs in only as an '
                        'ordinary customer, never as staff or as the platform operator')
    if not isinstance(persona.get('tenant'), (str, int)) or isinstance(persona.get('tenant'), bool):
        problems.append(f'{where}: "tenant" (the bank, company or account the customer '
                        'belongs to) is required')
    login = persona.get('login')
    if not isinstance(login, dict):
        problems.append(f'{where}: "login" must be an object with method, path and body')
    else:
        if str(login.get('method', '')).upper() not in LOGIN_METHODS:
            problems.append(f'{where}: login "method" must be one of {", ".join(sorted(LOGIN_METHODS))}')
        path = login.get('path')
        if not isinstance(path, str) or not _PATH_RE.match(path) or '@' in path:
            problems.append(f'{where}: login "path" must start with a single "/" and name a '
                            'path on the target, not another host')
        if not isinstance(login.get('body'), dict):
            problems.append(f'{where}: login "body" must be a JSON object')
    token = persona.get('token')
    if not isinstance(token, dict) or len(token) != 1 or not (
            isinstance(token.get('json'), str) and (token['json'] == '' or token['json'].startswith('/'))
            or isinstance(token.get('cookie'), str) and _HEADER_RE.match(token['cookie'])):
        problems.append(f'{where}: "token" must be {{"json": "<JSON pointer>"}} or '
                        '{"cookie": "<name>"}: where the session comes back in the login response')
    send = persona.get('send')
    if not isinstance(send, dict):
        problems.append(f'{where}: "send" must be {{"header": ..., "scheme": ...}} or {{"cookie": ...}}')
    elif 'header' in send:
        header = send.get('header')
        if (not isinstance(header, str) or not _HEADER_RE.match(header)
                or header.lower() in _RESERVED_HEADERS or set(send) - {'header', 'scheme'}
                or not isinstance(send.get('scheme', ''), str)
                or not _HEADER_RE.match(send.get('scheme') or 'x')):
            problems.append(f'{where}: "send.header" must be a plain header name other than '
                            f'{", ".join(sorted(_RESERVED_HEADERS))}, with an optional "scheme"')
    elif set(send) != {'cookie'} or not isinstance(send['cookie'], str) or not _HEADER_RE.match(send['cookie']):
        problems.append(f'{where}: "send.cookie" must be a cookie name')
    return problems


def load_identities(path: str, project_root: str) -> list[dict]:
    """The personas of a run, validated. The file holds credentials, so it must
    live outside the project, where nothing commits it."""
    if _within(path, project_root):
        raise IdentityError(f'The personas file {path} is inside the project. It holds '
                            'passwords: keep it outside the project tree, where it cannot '
                            'be committed.')
    try:
        with open(path, encoding='utf-8') as fh:
            document = json.load(fh)
    except OSError as exc:
        raise IdentityError(f'Could not read the personas file {path}: {exc.strerror}') from exc
    except ValueError as exc:
        raise IdentityError(f'The personas file {path} is not valid JSON (line {getattr(exc, "lineno", "?")}).') from exc
    personas = document.get('personas') if isinstance(document, dict) else None
    if not isinstance(personas, list) or not personas:
        raise IdentityError('The personas file needs a non-empty "personas" list.')
    problems = [problem for index, persona in enumerate(personas)
                for problem in _check_persona(index, persona)]
    names = [p.get('name') for p in personas if isinstance(p, dict)]
    if len(names) != len(set(names)):
        problems.append('persona names must be unique')
    if not problems:
        tenants: dict[str, int] = {}
        for persona in personas:
            tenants[str(persona['tenant'])] = tenants.get(str(persona['tenant']), 0) + 1
        if max(tenants.values()) < 2 or len(tenants) < 2:
            problems.append('the comparison needs two customers of the same tenant and one '
                            'of another tenant')
    if problems:
        raise IdentityError('The personas file cannot be used:\n  - ' + '\n  - '.join(problems))
    for persona in personas:
        persona['login']['method'] = persona['login']['method'].upper()
    return personas


def secrets_of(personas: list[dict]) -> list[str]:
    """Every string value of the login bodies: what a report must never show."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node:
            found.append(node)

    for persona in personas:
        walk(persona['login']['body'])
    return sorted(set(found), key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def build_catalog(project_root: str) -> tuple[list[dict], list[dict]]:
    """The routes the code declares, deduplicated, and the declarations the
    reader could not understand (reported, never dropped)."""
    tree = surface.Tree(project_root)
    routes, unknown = surface.read_routes(tree)
    catalog: dict[tuple[str, str], dict] = {}
    for route in routes:
        key = (route.method, surface.normalize(route.path))
        catalog.setdefault(key, {'method': key[0], 'path': key[1],
                                 'file': route.file, 'line': route.line})
    return (sorted(catalog.values(), key=lambda r: (r['path'], r['method'])),
            [{'file': u.file, 'line': u.line, 'reason': u.reason} for u in unknown])


def compare(catalog: list[dict], operations: list[dict], prefix: str) -> tuple[list[dict], list[dict]]:
    """Routes declared in the code and missing from the document, and
    documented operations no code declaration matches."""
    code = {(r['method'], path_shape(prefix + r['path'])) for r in catalog}
    documented = {(op['method'], path_shape(op['path'])) for op in operations}
    code_only = [r for r in catalog if (r['method'], path_shape(prefix + r['path'])) not in documented]
    doc_only = [op for op in operations if (op['method'], path_shape(op['path'])) not in code]
    return code_only, sorted(doc_only, key=lambda op: (op['path'], op['method']))


def _parent(path: str) -> str | None:
    """The part of a path before its first parameter, when it has one."""
    segments = path.split('/')
    for index, segment in enumerate(segments):
        if segment.startswith('{'):
            return '/'.join(segments[:index]) or '/'
    return None


def coverage(catalog: list[dict], lists: dict[str, dict[str, dict]],
             personas: list[dict]) -> tuple[list[dict], list[dict]]:
    """Routes with a path parameter that can be aimed at a resource whose owner
    is known (a list route above them returned resources for at least one
    persona), and those that cannot, with the reason."""
    covered, uncovered = [], []
    for route in catalog:
        parent = _parent(route['path'])
        if parent is None:
            continue
        returned = lists.get(parent) or {}
        owners = [p['name'] for p in personas if (returned.get(p['name']) or {}).get('count')]
        if owners:
            covered.append({**route, 'owners': owners})
        elif not returned:
            uncovered.append({**route, 'reason': f'no list route GET {parent} in the code'})
        else:
            statuses = sorted({str(v.get('status')) for v in returned.values()})
            uncovered.append({**route, 'reason': f'GET {parent} returned no resource for any '
                                                 f'persona (status {", ".join(statuses)})'})
    return covered, uncovered


def format_report(target_url: str, catalog: list[dict], unknown: list[dict], result: dict,
                  personas: list[dict]) -> str:
    prefix = result.get('prefix') or ''
    lines = [(f'Identity catalog of {target_url} (signed in as {len(personas)} customers; no '
              'request other than the logins and the list routes was sent)'), '']
    lines.append('Sessions:')
    for session in result.get('sessions') or []:
        state = 'signed in' if session.get('signed_in') else 'NOT signed in'
        lines.append(f'  - {session.get("persona")}: {state} (login status {session.get("status")})')
    operations = result.get('operations') or []
    lines += ['', f'Routes declared in the code: {len(catalog)}'
              + (f' (served under {prefix})' if prefix else '')]
    if result.get('openapi_path'):
        code_only, doc_only = compare(catalog, operations, prefix)
        lines.append(f'OpenAPI document: {result["openapi_path"]} ({len(operations)} operations)')
        for route in code_only:
            lines.append(f'  - [Low] declared in the code, missing from the document: '
                         f'{route["method"]} {prefix}{route["path"]} ({route["file"]}:{route["line"]})')
        for op in doc_only:
            lines.append(f'  - [Low] documented, no declaration in the code read: '
                         f'{op["method"]} {op["path"]}')
    else:
        lines.append('OpenAPI document: none found (pass --openapi-path if it is served elsewhere)')
    if unknown:
        lines += ['', f'Declarations the reader did not understand ({len(unknown)}), not in the catalog:']
        lines += [f'  - {u["file"]}:{u["line"]}: {u["reason"]}' for u in unknown]
    covered, uncovered = coverage(catalog, result.get('lists') or {}, personas)
    lines += ['', (f'Routes with a parameter: {len(covered) + len(uncovered)}; aimable at a '
                   f'resource whose owner is known: {len(covered)}')]
    for route in uncovered:
        lines.append(f'  - not covered: {route["method"]} {prefix}{route["path"]}: {route["reason"]}')
    return redact('\n'.join(lines), secrets_of(personas))
