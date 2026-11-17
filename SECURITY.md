# Security policy

## Reporting a vulnerability

Report it privately through GitHub: the repository's **Security** tab,
**Report a vulnerability**. Do not open a public issue, pull request or
discussion for it.

Include what is affected (a hook, a gate, the local lab, an adapter), the
steps to reproduce it, and what an attacker gains. A gate that can be
bypassed counts: the playbook's hooks are security controls.

You get an answer within 7 days. A confirmed issue is fixed on `develop`,
released on `main`, and credited to you in the release notes unless you ask
otherwise.

## Supported versions

Only the latest release on `main` gets fixes.

## Scope

In scope: the hooks in `hooks/`, the local validation lab in `lab/`, the
adapters in `adapters/`, the install scripts, and the workflows in
`.github/`.

Out of scope: findings that need an already compromised machine or a
malicious local user, and the documented known gaps of the gates (listed in
`tests/unit/test_gate_evasion.py`, `KNOWN_GAPS`): those are text checks with
stated limits, not boundaries.
