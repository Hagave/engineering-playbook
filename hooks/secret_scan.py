#!/usr/bin/env python3
"""
Secret detection engine for the playbook hooks.

The rules are gitleaks' (MIT), converted once by scripts/sync_secret_rules.py
into hooks/data/secret_rules.json, so the hooks need neither a TOML parser nor
the gitleaks binary. This module only answers "which secrets are in this text";
the decisions — what to scan, when to deny, how to fail — live with the other
gates in playbook_core.

Cost: every hook call is a fresh Python process, so nothing is compiled up
front. The JSON is read on the first scan only, and a rule's regex is compiled
only when one of its keywords occurs in the text — the same prefilter gitleaks
uses, and the reason a typical edit costs a few milliseconds instead of 200+
compilations.
"""
import functools
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'secret_rules.json')

# The user's exceptions, one fingerprint per line, relative to the repository
# root. Only the user writes it: playbook_core denies the model any write there.
ALLOW_FILE = os.path.join('.playbook', 'secrets-allow')

TIER_DENY = 'deny'
TIER_WARN = 'warn'
ACTIVE_TIERS = (TIER_DENY, TIER_WARN)

# Bound on how much text one scan reads. A larger input is reported as not
# scanned (see `scan_text`) so the caller decides how to fail, instead of the
# hook silently timing out.
MAX_SCAN_CHARS = 4_000_000

# When the hook process must have answered by (time.monotonic()), set by
# playbook_core.start_hook_clock. Size alone does not bound a scan: dense text
# with a rule keyword ran ~10 s per MB, so a 2 MB write outlived the hook's
# 20 s timeout, and a killed hook lets the tool call run. Past the deadline a
# scan returns None (not scanned) and the caller fails as it does for size.
_deadline: float | None = None


def set_deadline(deadline: float | None) -> None:
    global _deadline
    _deadline = deadline


def _out_of_time() -> bool:
    return _deadline is not None and time.monotonic() >= _deadline

# The rule file itself, by its repository-relative path in this repository:
# its allowlist regexes and stopwords spell out fragments of real token
# shapes, so scanning it reports the rules as secrets. Matched exactly, not as
# a suffix: any other project's `.../hooks/data/secret_rules.json` is scanned.
_OWN_DATA = 'hooks/data/secret_rules.json'


@dataclass(frozen=True)
class Finding:
    rule_id: str
    tier: str
    path: str
    secret: str
    commit: str = ''

    @property
    def fingerprint(self) -> str:
        """Stable id for the allow file: the rule plus a hash of the value.

        Keyed on the value, not on path and line, so an allowed fixture stays
        allowed when the file moves or lines shift above it."""
        digest = hashlib.sha256(self.secret.encode('utf-8', 'replace')).hexdigest()[:16]
        return f'{self.rule_id}:{digest}'

    @property
    def redacted(self) -> str:
        """What a message may show: never enough of the value to use it."""
        return f'{self.secret[:4]}... ({len(self.secret)} chars)'


class _Allowlist:
    def __init__(self, raw: dict) -> None:
        self.condition = str(raw.get('condition', 'OR')).upper()
        self.regex_target = str(raw.get('regex_target', 'secret'))
        self._regexes = tuple(raw.get('regexes', ()))
        self._paths = tuple(raw.get('paths', ()))
        self.stopwords = tuple(raw.get('stopwords', ()))

    @functools.cached_property
    def regexes(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(r) for r in self._regexes)

    @functools.cached_property
    def paths(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(p) for p in self._paths)

    def path_allowed(self, path: str) -> bool:
        return bool(path) and any(p.search(path) for p in self.paths)

    def allows(self, secret: str, match: str, line: str, path: str) -> bool:
        checks: list[bool] = []
        if self._regexes:
            target = {'match': match, 'line': line}.get(self.regex_target, secret)
            checks.append(any(r.search(target) for r in self.regexes))
        if self._paths:
            checks.append(self.path_allowed(path))
        if self.stopwords:
            lowered = secret.lower()
            checks.append(any(word in lowered for word in self.stopwords))
        if not checks:
            return False
        return all(checks) if self.condition == 'AND' else any(checks)


class _Rule:
    def __init__(self, raw: dict) -> None:
        self.rule_id = str(raw['id'])
        self.tier = str(raw.get('tier', TIER_WARN))
        self._regex = raw.get('regex')
        self._path = raw.get('path')
        self.secret_group = int(raw.get('secret_group', 0))
        self.entropy = float(raw.get('entropy', 0.0))
        self.keywords = tuple(raw.get('keywords', ()))
        self.allowlists = tuple(_Allowlist(a) for a in raw.get('allowlists', ()))

    @functools.cached_property
    def regex(self) -> re.Pattern[str] | None:
        return re.compile(self._regex) if self._regex else None

    @functools.cached_property
    def path_re(self) -> re.Pattern[str] | None:
        return re.compile(self._path) if self._path else None

    def secret_of(self, match: re.Match[str]) -> str:
        """The captured secret: the declared group, else the only group, else
        the whole match — the order gitleaks resolves it in."""
        if self.secret_group:
            return match.group(self.secret_group) or ''
        if match.re.groups == 1:
            return match.group(1) or ''
        return match.group(0)


@functools.lru_cache(maxsize=1)
def _ruleset() -> tuple[tuple[_Rule, ...], _Allowlist] | None:
    """The converted rules, or None when the data file is missing or corrupt
    (a broken install: scanning then finds nothing, and the structural tests
    are what catch it)."""
    try:
        with open(RULES_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
        rules = tuple(_Rule(r) for r in data['rules'])
        return rules, _Allowlist(data.get('global_allowlist', {}))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def rules_loaded() -> bool:
    return _ruleset() is not None


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    size = len(value)
    return -sum(n / size * math.log2(n / size) for n in counts.values())


def _line_at(text: str, start: int, end: int) -> str:
    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', end)
    return text[line_start:line_end if line_end != -1 else len(text)]


def scan_text(text: str, path: str = '', tiers: tuple[str, ...] = ACTIVE_TIERS,
              commit: str = '') -> list[Finding] | None:
    """Secrets in `text`, one finding per (rule, value).

    `path` is the file the text belongs to, relative to the repository root:
    gitleaks' path allowlists (lockfiles, vendored code, binaries) and path
    rules need it. None when the text is larger than MAX_SCAN_CHARS or the
    hook's deadline passes mid-scan (see `set_deadline`) — not scanned is not
    the same as clean, and the caller owns that decision."""
    if not text:
        return []
    ruleset = _ruleset()
    if ruleset is None:
        return []
    rules, global_allow = ruleset
    # A path the rules exempt is never scanned, however large: checked after
    # the size cap, a regenerated lockfile past it made every push "too large
    # to scan" and the push gate denied it on every retry.
    if global_allow.path_allowed(path) or path.replace(os.sep, '/') == _OWN_DATA:
        return []
    if len(text) > MAX_SCAN_CHARS:
        return None

    lowered = text.lower()
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        if _out_of_time():
            return None
        if rule.tier not in tiers:
            continue
        if rule.path_re is not None and not (path and rule.path_re.search(path)):
            continue
        if rule.regex is None:
            # A path-only rule (a keystore file): the file itself is the finding.
            if rule.path_re is not None and (rule.rule_id, path) not in seen:
                seen.add((rule.rule_id, path))
                findings.append(Finding(rule.rule_id, rule.tier, path, path, commit))
            continue
        if rule.keywords and not any(k in lowered for k in rule.keywords):
            continue
        for match in rule.regex.finditer(text):
            secret = rule.secret_of(match)
            if not secret or (rule.rule_id, secret) in seen:
                continue
            if rule.entropy and shannon_entropy(secret) < rule.entropy:
                continue
            line = _line_at(text, match.start(), match.end())
            if global_allow.allows(secret, match.group(0), line, path):
                continue
            if any(a.allows(secret, match.group(0), line, path) for a in rule.allowlists):
                continue
            seen.add((rule.rule_id, secret))
            findings.append(Finding(rule.rule_id, rule.tier, path, secret, commit))
    # A value a precise rule already denies also matches the generic ones;
    # reporting it twice, once as a "possible" secret, only blurs the verdict.
    denied = {f.secret for f in findings if f.tier == TIER_DENY}
    return [f for f in findings if f.tier == TIER_DENY or f.secret not in denied]


def added_lines(old: str, new: str) -> str:
    """Lines of `new` that `old` does not contain.

    What a write ADDS is what it can leak. Scanning the whole new text would
    deny any edit to a file that already holds an (allowed or pre-existing)
    value, which is a different problem with a different owner."""
    if not old:
        return new
    existing = set(old.splitlines())
    return '\n'.join(line for line in new.splitlines() if line not in existing)


def scan_patch(patch: str, tiers: tuple[str, ...] = ACTIVE_TIERS) -> list[Finding] | None:
    """Secrets on the added lines of a unified diff, or of `git log -p`.

    A `commit <sha>` line (from `--format=commit %h`) starts a commit; a hunk's
    `+` lines are what it adds. Lines before the first `@@` of a file are
    headers — that is how a `+++ b/path` header is told apart from an added
    line that happens to start with `++`."""
    findings: list[Finding] = []
    commit = path = ''
    in_hunk = False
    added: list[str] = []

    def flush() -> bool:
        if added:
            found = scan_text('\n'.join(added), path, tiers, commit)
            if found is None:
                return False
            findings.extend(found)
            added.clear()
        return True

    for line in patch.splitlines():
        if line.startswith('commit ') and not in_hunk or line.startswith('diff --git '):
            if not flush():
                return None
            in_hunk = False
            if line.startswith('commit '):
                commit = line[len('commit '):].strip()
            path = ''
        elif not in_hunk and line.startswith('+++ '):
            target = line[4:].strip()
            path = '' if target == '/dev/null' else (target[2:] if target.startswith('b/') else target)
        elif line.startswith('@@'):
            in_hunk = True
        elif in_hunk and line.startswith('+'):
            added.append(line[1:])
        elif in_hunk and line.startswith('commit '):
            # `-U0` hunks carry only +/- lines, so an unprefixed `commit` line
            # is the next commit's header.
            if not flush():
                return None
            in_hunk = False
            commit, path = line[len('commit '):].strip(), ''
    if not flush():
        return None
    return findings


def load_allowed(repo_root: str) -> frozenset[str]:
    """Fingerprints the user allowed, from `<repo>/.playbook/secrets-allow`.

    One fingerprint per line; `#` starts a comment, so a line can say why."""
    try:
        with open(os.path.join(repo_root, ALLOW_FILE), encoding='utf-8') as fh:
            lines = fh.read().splitlines()
    except OSError:
        return frozenset()
    allowed = set()
    for line in lines:
        value = line.split('#', 1)[0].strip()
        if value:
            allowed.add(value.split()[0])
    return frozenset(allowed)


def redact(text: str) -> str:
    """`text` with every deny-tier secret cut down to its first four characters.

    For text that is persisted and read back (the failure journal): a scan
    error leaves the text as it was, because the keyword redaction in
    playbook_core.scrub_secrets runs as well."""
    found = scan_text(text, tiers=(TIER_DENY,))
    for finding in found or ():
        text = text.replace(finding.secret, f'{finding.secret[:4]}***')
    return text
