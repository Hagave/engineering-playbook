#!/usr/bin/env python3
"""
Apply the repository's protection on GitHub: the rulesets in .github/rulesets/
and the repository settings below. Dry run by default; `--apply` changes
GitHub.

    python3 scripts/apply_repo_protection.py            # show what would change
    python3 scripts/apply_repo_protection.py --apply    # apply it

Needs the GitHub CLI (`gh`) logged in as a repository admin. Rulesets on a
private repository need GitHub Pro; on a public one they are free, so this
runs as part of making the repository public.

Idempotent: a ruleset whose name already exists is updated in place, never
duplicated. The JSON files are the source of truth; a rule changed by hand on
GitHub is overwritten on the next run.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULESETS_DIR = os.path.join(ROOT, '.github', 'rulesets')

# Merges keep the history the repository already has (merge commits); a
# merged branch is deleted on GitHub.
REPO_SETTINGS = {
    'delete_branch_on_merge': True,
    'allow_merge_commit': True,
    'allow_squash_merge': False,
    'allow_rebase_merge': False,
    'allow_auto_merge': False,
}
# Free on public repositories: block a push that carries a known secret
# pattern, and alert on any that got in.
SECURITY_AND_ANALYSIS = {
    'secret_scanning': {'status': 'enabled'},
    'secret_scanning_push_protection': {'status': 'enabled'},
}
# PUT endpoints that switch a feature on: Dependabot alerts, and private
# vulnerability reporting (SECURITY.md points reporters to it).
FEATURE_ENDPOINTS = ('vulnerability-alerts', 'private-vulnerability-reporting')


class ApplyError(Exception):
    """A GitHub call that failed; the message says which and why."""


def gh(args: list[str], payload: dict | None = None) -> object:
    command = ['gh', 'api', *args]
    if payload is not None:
        command += ['--input', '-']
    try:
        done = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise ApplyError('The GitHub CLI (`gh`) is not installed.') from exc
    except subprocess.TimeoutExpired as exc:
        raise ApplyError(f'`gh api {" ".join(args)}` timed out.') from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()
        if 'Upgrade to GitHub Pro' in detail:
            detail += ('\nRulesets on a private repository need GitHub Pro; make the repository '
                       'public first, or upgrade.')
        raise ApplyError(f'`gh api {" ".join(args)}` failed: {detail}')
    return json.loads(done.stdout) if done.stdout.strip() else None


def current_repo() -> str:
    try:
        done = subprocess.run(['gh', 'repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'],
                              capture_output=True, text=True, timeout=30, cwd=ROOT)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ApplyError('Could not ask `gh` which repository this is.') from exc
    if done.returncode != 0 or not done.stdout.strip():
        raise ApplyError('Could not resolve the repository; pass --repo owner/name.')
    return done.stdout.strip()


def load_rulesets() -> list[dict]:
    rulesets = []
    for path in sorted(glob.glob(os.path.join(RULESETS_DIR, '*.json'))):
        with open(path, encoding='utf-8') as fh:
            ruleset = json.load(fh)
        if not isinstance(ruleset, dict) or not ruleset.get('name'):
            raise ApplyError(f'{path}: a ruleset needs a "name".')
        rulesets.append(ruleset)
    if not rulesets:
        raise ApplyError(f'No ruleset files in {RULESETS_DIR}.')
    return rulesets


def plan(repo: str, rulesets: list[dict], existing: dict[str, int]) -> list[tuple[str, list[str], dict | None]]:
    """Every call to make, as (description, gh api args, payload)."""
    steps: list[tuple[str, list[str], dict | None]] = []
    for ruleset in rulesets:
        name = ruleset['name']
        if name in existing:
            steps.append((f'update ruleset "{name}"', ['-X', 'PUT', f'repos/{repo}/rulesets/{existing[name]}'], ruleset))
        else:
            steps.append((f'create ruleset "{name}"', ['-X', 'POST', f'repos/{repo}/rulesets'], ruleset))
    settings = {**REPO_SETTINGS, 'security_and_analysis': SECURITY_AND_ANALYSIS}
    steps.append(('repository settings: merge commits only, delete merged branches, secret scanning '
                  'and push protection', ['-X', 'PATCH', f'repos/{repo}'], settings))
    for endpoint in FEATURE_ENDPOINTS:
        steps.append((f'enable {endpoint}', ['-X', 'PUT', f'repos/{repo}/{endpoint}'], None))
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Apply branch/tag rulesets and repository settings.')
    parser.add_argument('--repo', help='owner/name (default: this checkout\'s GitHub repository)')
    parser.add_argument('--apply', action='store_true', help='change GitHub (default: dry run)')
    args = parser.parse_args(argv)
    try:
        repo = args.repo or current_repo()
        rulesets = load_rulesets()
        try:
            listed = gh([f'repos/{repo}/rulesets', '--paginate']) or []
        except ApplyError as exc:
            if args.apply:
                raise
            print(f'note: cannot list rulesets yet, so every ruleset reads as new. {exc}')
            listed = []
        existing = {r['name']: r['id'] for r in listed if isinstance(r, dict)}
        for description, call, payload in plan(repo, rulesets, existing):
            print(f'{"apply" if args.apply else "would"}: {description}  ({" ".join(call)})')
            if args.apply:
                gh(call, payload)
    except ApplyError as exc:
        print(f'[protection] {exc}', file=sys.stderr)
        return 1
    if not args.apply:
        print('Dry run: nothing changed. Re-run with --apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
