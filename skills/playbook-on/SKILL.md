---
name: playbook-on
description: "Re-activate engineering-playbook governance rules after they were suspended. Use when the user says /playbook-on, enable playbook, turn on playbook, or re-enable engineering governance."
version: 3.0.1
user-invocable: true
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Engineering Playbook — Re-activated

Engineering-playbook governance rules are **active** for the remainder of this session.

All non-negotiable rules apply immediately:

- No duplication — check registries and codebase before creating any artifact
- No dead code — remove what your changes made unused
- No hallucination — every decision traces to the task, the code, or a playbook rule
- No hardcoded secrets
- No N+1 in loops, no unbounded queries
- Act only on the decisions you own — fixed ones as they stand, delegated ones decided and recorded, the rest asked; never silently pick an interpretation
- Scope discipline — every changed line traces to the user's request

Skill routing is active. Load specific skills on demand per the routing table in `engineering-playbook`.

To suspend: `/playbook-off`

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
