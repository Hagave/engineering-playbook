# Pilot measurement: model, effort and the playbook on one bug survey

Measured on 2026-11-18 with playbook 4.1.0 (`15c61b8`) and Claude Code 2.1.328.
One task, one run per configuration. Read it as a case study, not a benchmark:
the numbers say what happened here and nothing about variance.

## The task

A bug survey of one module of a pilot project (Java/Spring backend, Angular
front end, local Docker stack): archiving, pinning and muting inbox
conversations, mirrored to a messaging gateway. The project's own rules define a
survey as reading the code, probing every route per role, clicking through the
screens, writing the module notes and presenting the findings, with no branch or
code until the user decides.

Every run opened a session in the project and was told only "pick up where we
left off". The task itself came from what the session loads on its own: the
project memory and `CLAUDE.md`.

## Reference list

An interactive Opus 5.5 run at high effort produced 12 findings: 10 in the
module and 2 outside it. 7 of them were reproduced (API probe or screen), the
rest read in code. Coverage below counts how many of those 12 each run also
reported: a full match counts 1, a partial match counts 0.5. Findings that are
not on the list are counted apart. They were not verified, except where marked
as reproduced.

## Results

| Run | Cost | Time | Requests | Coverage of the 12 | Reproduced | Findings not on the list | Cost per distinct finding |
|---|---|---|---|---|---|---|---|
| Opus 5.5 high (reference) | $7.13 | 1094 s | 113 | 12 | 7 | 0 | $0.59 |
| Sonnet 5.5 high | $1.69 | 933 s | 54 | 4.5 (38 %) | 3 | 3, one reproduced (a 500 the reference missed) | $0.23 |
| Sonnet 5.5 medium, 1st turn | $0.60 | 120 s | 20 | 2.5 (21 %) | 0 | 1 | $0.17 |
| Sonnet 5.5 medium, 2nd turn added | $1.77 total | 1337 s total | 63 | 3 (25 %) | 3 | 3 | $0.30 |
| Haiku 5.5 high | $0.28 | 250 s | 41 | 3 (25 %) | 0 | 2 | $0.06 |
| Haiku 5.5 medium | $0.27 | 226 s | 34 | 1.5 (13 %) | 0 | 0 | $0.18 |

"Distinct finding" = coverage + findings not on the list.

What each run did:

- **Sonnet medium** stopped after reading code and asked whether to run the
  probes, although the project rules already answer that. Told "yes" in a second
  turn, it probed and clicked through the screens. It still missed three
  findings whose code it had read in the first turn: an HTTP call held inside a
  database transaction, mute state that depends on a list being loaded, and a
  controller rule. Over two turns it cost more than Sonnet high and found less.
- **Sonnet high** finished the survey in one turn. It built a mock of the
  gateway, measured the lock held by the transaction and wrote the notes.
- **Haiku medium** reached the playbook's 70 % context threshold, wrote a
  handoff and stopped before any notes.
- **Haiku high** reported from code only. It downloaded two files that came back
  as 404 pages and looped on the Stop hook before ending.
- **Every finding that only the reference reported** needed the screen or a
  close reading of the front end: an archived e-mail conversation that cannot be
  found again, a menu item with no action, and the front end unarchiving what
  the server kept archived.
- **No run** created a branch or a commit, or changed `CLAUDE.md`.

No run made a claim that the reference's evidence contradicts. The differences
are in how much each run covered and how strong its evidence was.
The one wrong framing: both medium runs presented "unarchive archives on the
phone" as a current bug. Today every call is already rejected for a wrong
field name. The unarchive bug would only appear once someone fixes that field.

## Playbook on and off

Same model, effort, prompt and starting state. The only change is whether
`--plugin-dir` loads the playbook.

| Sonnet 5.5 medium, one turn | Cost | Time | Coverage | Notes written | Gateway contract checked |
|---|---|---|---|---|---|
| Playbook on | $0.60 | 120 s | 2.5 | yes | yes, against the gateway's source |
| Playbook off | $0.56 | 112 s | 1 | no | no, left as "to confirm" |

Both runs read code only and stopped to ask whether to continue. The early stop
belongs to the model at this effort, not to the playbook. With one run per
cell, the coverage gap (2.5 against 1) is a signal, not a result.

## What this supports and what it does not

Supported in this case:

- For a survey of this kind, effort mattered more than the model's price tier.
  Sonnet high reached 38 % of the reference, plus three findings the reference
  lacked, for 24 % of its cost. Sonnet medium needed a second turn and ended up
  costlier than Sonnet high.
- The Haiku runs cost about 4 % of the reference and covered at most a quarter
  of it, with nothing reproduced. That is triage, not a survey.
- What needs a screen or a cross-check between front end and backend went
  missing below Opus high.

Not supported:

- Any percentage for the playbook's effect. The on/off comparison is one run
  per cell, at the effort where the model stops early either way.
- Generalization beyond this module and project.

## Method

- **Cost.** Tokens per request from the session transcript, times the published
  price per token, with cache writes and cache reads priced separately. For
  every headless run, this matched the cost Claude Code reports itself within
  $0.02. The reference's cost and time count until its last answer. A later
  notification-only request ($0.06) is excluded.
- **Isolation.** Before each run, project memory, `CLAUDE.md` and the notes
  folder were reset to a snapshot taken before the reference run. Files that a
  run created were moved out after it.
- **Effort and model.** Confirmed per request in each transcript.

## Limitations

- One run per configuration.
- The reference is itself a run with the playbook on, so the list is not
  neutral.
- The database and running processes were not isolated. Later runs could see
  probe data left by the reference run, and one run noticed a mock left by
  another run.
- The "playbook off" run still loads the project `CLAUDE.md`, much of which the
  playbook's learning loop wrote over earlier sessions. It isolates what the
  playbook does at run time (gates, routing, context guard), not its whole
  effect.
