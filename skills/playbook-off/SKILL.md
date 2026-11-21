---
name: playbook-off
description: "Suspend engineering-playbook governance rules for this session. Use when the user says /playbook-off, disable playbook, turn off playbook, suspend rules, or explicitly wants to work without engineering governance for this session."
version: 3.0.1
user-invocable: true
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Engineering Playbook — Suspended

Engineering-playbook governance rules are **suspended** for the remainder of this session.

The following are no longer enforced:
- Non-negotiable rules (duplication, dead code, scope discipline, evidence policy)
- Skill routing on-demand
- Registry protocol
- Validation checklist before responses

To re-activate at any point: `/playbook-on`

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
