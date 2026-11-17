#!/usr/bin/env python3
"""
Regenerate hooks/data/secret_rules.json from a pinned gitleaks rule file.

The hooks read only the JSON this writes, so the runtime needs no TOML parser
and no network. This script needs Python 3.11+ (tomllib) and runs only when the
rules are re-synced:

    gh api 'repos/gitleaks/gitleaks/contents/config/gitleaks.toml?ref=v8.30.1' \\
        --jq .content | base64 -d > /tmp/gitleaks.toml
    python3 scripts/sync_secret_rules.py /tmp/gitleaks.toml v8.30.1

Two things happen besides the format change:

1. Go RE2 syntax that Python's `re` rejects is rewritten (a mid-pattern `(?i)`,
   `\\z`, POSIX classes). Every converted pattern is compiled here, so a rule
   that does not translate fails the sync instead of failing silently in a hook.
2. Every rule gets a tier: `deny`, `warn` or `off`. See `_tier`.

gitleaks is MIT licensed; its notice ships in hooks/data/LICENSE.gitleaks.
"""
import json
import os
import re
import sys
import warnings

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_ROOT, 'hooks', 'data', 'secret_rules.json')

# gitleaks generates keyword-anchored rules with this prefix: the secret is
# recognized by the word next to it ("api_key = ..."), not by its own shape.
# Those fire on test values and placeholders far more often, so they warn.
_KEYWORD_ANCHORED_RE = re.compile(r'\[\\w\.-\]\{0,50\}\?\(\?:')

# Not secrets: identifiers and public keys that are meant to be shared.
_OFF = frozenset({
    'adobe-client-id', 'asana-client-id', 'bitbucket-client-id', 'discord-client-id',
    'flutterwave-public-key', 'linkedin-client-id', 'lob-pub-api-key', 'looker-client-id',
    'mailgun-pub-key', 'messagebird-client-id', 'new-relic-user-api-id', 'plaid-client-id',
    'sendbird-access-id', 'sumologic-access-id',
})

# Distinctive shape, but common in tests, docs and fixtures, or matched by a
# shape too loose to deny on.
_WARN = frozenset({
    'curl-auth-header', 'curl-auth-user', 'easypost-test-api-token', 'generic-api-key',
    'jwt', 'jwt-base64', 'kubernetes-secret-yaml', 'nuget-config-password', 'pkcs12-file',
    'sidekiq-sensitive-url', 'twilio-api-key',
})

_POSIX_CLASSES = {
    '[:alnum:]': 'A-Za-z0-9', '[:alpha:]': 'A-Za-z', '[:digit:]': '0-9',
    '[:xdigit:]': '0-9A-Fa-f', '[:upper:]': 'A-Z', '[:lower:]': 'a-z',
    '[:space:]': r'\s', '[:word:]': r'\w',
}


def _tier(rule: dict) -> str:
    rule_id = rule['id']
    if rule_id in _OFF:
        return 'off'
    if rule_id in _WARN:
        return 'warn'
    if _KEYWORD_ANCHORED_RE.search(rule.get('regex', '')[:40]):
        return 'warn'
    return 'deny'


def _scope_inline_flags(pattern: str) -> str:
    """Rewrite a mid-pattern `(?i)` as a scoped `(?i:...)`.

    In RE2 a flag group applies until the end of the group it appears in;
    Python 3.11+ accepts global flags only at the very start. The scope is
    closed right before the enclosing group's `)`, or at the end of the pattern."""
    out: list[str] = []
    closers: list[int] = [0]  # per open group: how many scoped flags close there
    i = 0
    in_class = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == '\\':
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            if ch == ']':
                in_class = False
            out.append(ch)
            i += 1
            continue
        if ch == '[':
            in_class = True
            out.append(ch)
            i += 1
            # A `]` right after `[` or `[^` is a literal, not the end of the class.
            if pattern[i:i + 1] == '^':
                out.append('^')
                i += 1
            if pattern[i:i + 1] == ']':
                out.append(']')
                i += 1
            continue
        if pattern.startswith('(?i)', i) and i > 0:
            out.append('(?i:')
            closers[-1] += 1
            i += 4
            continue
        if ch == '(':
            closers.append(0)
        elif ch == ')':
            out.append(')' * closers.pop())
        out.append(ch)
        i += 1
    out.append(')' * closers[0])
    return ''.join(out)


def to_python_regex(pattern: str) -> str:
    """Translate a gitleaks (Go RE2) pattern into one Python's `re` accepts."""
    for posix, ranges in _POSIX_CLASSES.items():
        pattern = pattern.replace(posix, ranges)
    pattern = pattern.replace('\\z', '\\Z')
    return _scope_inline_flags(pattern)


def _compile_checked(pattern: str, where: str) -> str:
    converted = to_python_regex(pattern)
    with warnings.catch_warnings():
        warnings.simplefilter('error')  # a FutureWarning means a changed meaning later
        try:
            re.compile(converted)
        except (re.error, FutureWarning) as exc:
            raise SystemExit(f'{where}: does not translate ({exc}): {pattern!r}') from exc
    return converted


def _allowlist(raw: dict, where: str) -> dict:
    return {
        'condition': str(raw.get('condition', 'OR')).upper(),
        'regex_target': raw.get('regexTarget', 'secret'),
        'regexes': [_compile_checked(r, f'{where} regex') for r in raw.get('regexes', [])],
        'paths': [_compile_checked(p, f'{where} path') for p in raw.get('paths', [])],
        'stopwords': [s.lower() for s in raw.get('stopwords', [])],
    }


def convert(config: dict, ref: str) -> dict:
    rules = []
    for rule in config['rules']:
        rule_id = rule['id']
        converted: dict = {'id': rule_id, 'tier': _tier(rule)}
        if 'regex' in rule:
            converted['regex'] = _compile_checked(rule['regex'], rule_id)
        if 'path' in rule:
            converted['path'] = _compile_checked(rule['path'], f'{rule_id} path')
        if 'secretGroup' in rule:
            converted['secret_group'] = int(rule['secretGroup'])
        if 'entropy' in rule:
            converted['entropy'] = float(rule['entropy'])
        converted['keywords'] = [k.lower() for k in rule.get('keywords', [])]
        converted['allowlists'] = [
            _allowlist(a, f'{rule_id} allowlist') for a in rule.get('allowlists', [])
        ]
        rules.append(converted)
    return {
        'source': {
            'project': 'gitleaks', 'repository': 'https://github.com/gitleaks/gitleaks',
            'ref': ref, 'path': 'config/gitleaks.toml', 'license': 'MIT',
            'notice': 'hooks/data/LICENSE.gitleaks',
        },
        'global_allowlist': _allowlist(config.get('allowlist', {}), 'global allowlist'),
        'rules': rules,
    }


def main() -> int:
    if sys.version_info < (3, 11):
        print('sync_secret_rules.py needs Python 3.11+ (tomllib).', file=sys.stderr)
        return 1
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    import tomllib
    toml_path, ref = sys.argv[1], sys.argv[2]
    with open(toml_path, 'rb') as fh:
        config = tomllib.load(fh)
    data = convert(config, ref)
    os.makedirs(os.path.dirname(_OUT), exist_ok=True)
    with open(_OUT, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write('\n')
    tiers: dict[str, int] = {}
    for rule in data['rules']:
        tiers[rule['tier']] = tiers.get(rule['tier'], 0) + 1
    print(f'{len(data["rules"])} rules from gitleaks {ref} -> {_OUT}: {tiers}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
