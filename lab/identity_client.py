"""
The lab's identity client: runs inside the lab, on the internal network only,
against the isolated copy of the application. Standard library only.

Reads `input.json` from the mounted run directory, then:

0. waits for the target to answer HTTP at all: a container that is running is
   not yet a server that listens (a dev stack compiles, migrates and seeds
   first), and one that never answers is reported as such, not as a failed login;
1. looks for the application's OpenAPI document and lists its operations;
2. signs each persona in through the application's own login route;
3. as each persona, calls the catalog's list routes (GET, no path parameter)
   and records how many resources each returned, so the routes under them can
   be aimed at a resource whose owner is known.

Writes `result.json` next to it. No credential and no session token is ever
written: only status codes, counts and which routes are covered. No request
other than the OpenAPI lookups, the logins and those GETs is sent.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

RUN_DIR = os.environ.get('REDLAB_RUN_DIR', '/zap/wrk')
TIMEOUT_SECONDS = 10
DEFAULT_READY_SECONDS = 240
READY_POLL_SECONDS = 2
# A gateway in front of an app that is still booting answers these.
NOT_READY_STATUSES = frozenset({0, 502, 503, 504})
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_LIST_ROUTES = 200
MAX_WALK_NODES = 5000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def request(base_url: str, method: str, path: str, headers: dict[str, str] | None = None,
            body: object = None) -> tuple[int, list[tuple[str, str]], bytes]:
    """Status, every response header as (name, value) -- a repeated one such as
    Set-Cookie keeps each occurrence -- and body; status 0 when the target did
    not answer. Redirects are returned, never followed."""
    data = None
    sent = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        sent.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(base_url + path, data=data, method=method, headers=sent)
    try:
        with _OPENER.open(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, list(resp.headers.items()), resp.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, list(exc.headers.items()), exc.read(MAX_BODY_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return 0, [], b''


def json_pointer(document: object, pointer: str) -> object:
    """RFC 6901. None when the pointer does not resolve."""
    if pointer == '':
        return document
    current = document
    for raw in pointer.split('/')[1:]:
        token = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return None
    return current


def _cookie(headers: list[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() != 'set-cookie':
            continue
        first = value.split(';', 1)[0]
        cookie_name, _, cookie_value = first.partition('=')
        if cookie_name.strip() == name and cookie_value:
            return cookie_value.strip()
    return None


def sign_in(base_url: str, persona: dict) -> tuple[int, dict[str, str] | None]:
    """The login status and the headers that carry the session, or None when
    no session came back."""
    login = persona['login']
    status, headers, body = request(base_url, login['method'], login['path'],
                                    headers=login.get('headers'), body=login['body'])
    token_spec, send = persona['token'], persona['send']
    token = None
    if 'json' in token_spec:
        try:
            value = json_pointer(json.loads(body or b'null'), token_spec['json'])
        except ValueError:
            value = None
        token = value if isinstance(value, str) and value else None
    else:
        token = _cookie(headers, token_spec['cookie'])
    if not (200 <= status < 300) or token is None:
        return status, None
    if 'header' in send:
        scheme = send.get('scheme')
        return status, {send['header']: f'{scheme} {token}' if scheme else token}
    return status, {'Cookie': f'{send["cookie"]}={token}'}


def resource_ids(document: object) -> list[str]:
    """The `id` of each object in the first list of objects found in the
    response, breadth first: the list itself, or one wrapped in an envelope
    (`{"data": [...]}`, `{"items": [...]}`)."""
    queue = [document]
    seen = 0
    while queue and seen < MAX_WALK_NODES:
        node = queue.pop(0)
        seen += 1
        if isinstance(node, list) and node and all(isinstance(item, dict) for item in node):
            ids = [str(item['id']) for item in node
                   if isinstance(item.get('id'), (str, int)) and not isinstance(item.get('id'), bool)]
            if ids:
                return ids
        if isinstance(node, dict):
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return []


def openapi_operations(base_url: str, candidates: list[str]) -> tuple[str | None, list[dict]]:
    """The first candidate path that serves an OpenAPI document, and its
    operations as method and path."""
    verbs = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}
    for path in candidates:
        status, _, body = request(base_url, 'GET', path)
        if status != 200:
            continue
        try:
            document = json.loads(body)
        except ValueError:
            continue
        if not isinstance(document, dict) or not isinstance(document.get('paths'), dict):
            continue
        operations = [{'method': method.upper(), 'path': op_path}
                      for op_path, item in document['paths'].items() if isinstance(item, dict)
                      for method in item if method.lower() in verbs]
        return path, operations
    return None, []


def _has_parameter(path: str) -> bool:
    return any(segment.startswith('{') for segment in path.split('/'))


def path_shape(path: str) -> str:
    """A path with every parameter as `{}`, whatever its name. Catalog paths
    arrive normalized to `{name}`, and OpenAPI writes parameters the same way."""
    return '/'.join('{}' if segment.startswith('{') else segment for segment in path.split('/'))


def infer_prefix(catalog: list[dict], operations: list[dict]) -> str:
    """The path prefix the application serves the code's routes under (a
    global prefix, a version segment), read from the OpenAPI document: the
    prefix that turns the most code routes into documented ones, when that is
    more than the routes documented as declared."""
    documented = {(op['method'], path_shape(op['path'])) for op in operations}
    exact = sum(1 for route in catalog if (route['method'], path_shape(route['path'])) in documented)
    votes: dict[str, int] = {}
    for route in catalog:
        shape = path_shape(route['path']).rstrip('/')
        for method, doc_shape in documented:
            if method == route['method'] and shape and doc_shape.endswith(shape) and doc_shape != shape:
                prefix = doc_shape[:-len(shape)]
                if prefix.startswith('/') and not prefix.endswith('/') and '{}' not in prefix:
                    votes[prefix] = votes.get(prefix, 0) + 1
    if not votes or max(votes.values()) <= exact:
        return ''
    return max(votes, key=lambda p: (votes[p], -len(p)))


def wait_until_answering(base_url: str, ready_seconds: float) -> float | None:
    """Seconds until the target answered with a status other than a gateway's
    "not ready", or None when it had not once the time ran out."""
    start = time.monotonic()
    while True:
        if request(base_url, 'GET', '/')[0] not in NOT_READY_STATUSES:
            return time.monotonic() - start
        if time.monotonic() - start >= ready_seconds:
            return None
        time.sleep(READY_POLL_SECONDS)


def run(spec: dict) -> dict:
    base_url = spec['base_url']
    ready_seconds = spec.get('ready_seconds', DEFAULT_READY_SECONDS)
    if wait_until_answering(base_url, ready_seconds) is None:
        return {'reachable': False, 'openapi_path': None, 'operations': [], 'prefix': '',
                'sessions': [], 'lists': {}}
    openapi_path, operations = openapi_operations(base_url, spec.get('openapi_paths') or [])
    prefix = spec['prefix'] if spec.get('prefix') is not None else infer_prefix(spec['catalog'], operations)
    list_routes = [route for route in spec['catalog']
                   if route['method'] == 'GET' and not _has_parameter(route['path'])][:MAX_LIST_ROUTES]
    sessions = []
    lists: dict[str, dict[str, dict]] = {}
    for persona in spec['personas']:
        status, auth = sign_in(base_url, persona)
        sessions.append({'persona': persona['name'], 'status': status, 'signed_in': auth is not None})
        if auth is None:
            continue
        for route in list_routes:
            status, _, body = request(base_url, 'GET', prefix + route['path'], headers=auth)
            try:
                ids = resource_ids(json.loads(body)) if 200 <= status < 300 and body else []
            except ValueError:
                ids = []
            lists.setdefault(route['path'], {})[persona['name']] = {'status': status, 'count': len(ids)}
    return {'reachable': True, 'openapi_path': openapi_path, 'operations': operations,
            'prefix': prefix, 'sessions': sessions, 'lists': lists}


def main() -> int:
    with open(os.path.join(RUN_DIR, 'input.json'), encoding='utf-8') as fh:
        spec = json.load(fh)
    result = run(spec)
    with open(os.path.join(RUN_DIR, 'result.json'), 'w', encoding='utf-8') as fh:
        json.dump(result, fh)
    return 0


if __name__ == '__main__':
    sys.exit(main())
