# dev/observe

A local observability store for developing the playbook. It answers, with
data from real sessions, what happened when the model's code was not right
the first time: what it had read, where the fix landed, what kind of failure
it was, and what every hook returned along the way.

It is a development tool. Nothing the playbook ships references it, the
release archive leaves `dev/` out (`.gitattributes`), and it changes nothing
in a session: it only reads the transcripts Claude Code already writes.

## Use

```
python3 dev/observe/cli.py all            # ingest, derive, report
python3 dev/observe/cli.py all --days 7   # only transcripts touched this week
python3 dev/observe/cli.py report --by-project
```

Ingestion is incremental, so running it again reads only what is new. Claude
Code deletes old transcripts on its own schedule; ingest at least that often
to keep the history.

The store is one SQLite file, outside every repository and readable by its
owner only: `$PLAYBOOK_OBSERVE_DB`, or
`${XDG_STATE_HOME:-~/.local/state}/engineering-playbook/observe.db`. It holds
prompts and commands from every project on the machine, so it is never
committed, copied into an issue or attached to a pull request. Ask it
anything the report does not print (read-only):

```
python3 dev/observe/cli.py query \
  "SELECT cause, target_read, COUNT(*) FROM episodes WHERE kind = 'fix_loop' GROUP BY 1, 2"
```

## What is stored

| Table | One row per |
|---|---|
| `sessions` | session, and each subagent of it (`agent_id`) |
| `prompts` | message that opened or continued a request, with its source |
| `requests`, `api_calls`, `api_blocks` | API call: model, effort, token usage, thinking time |
| `tool_calls` | tool call with its outcome: error, exit code, denial, which check it ran |
| `reads` | file view, with the line range when known (Read, and `cat`/`sed -n`/`head`/`grep` in Bash) |
| `edits` | changed hunk (Edit, Write, and edits made through Bash), with what it changed and the code and comment lines it added (see `changes.py`) |
| `hook_events` | hook result: outcome, duration, and the playbook gate that spoke |
| `edited_files` | file a session edited: what had been read first, hunks, hunks rewritten |
| `rewrites` | hunk that rewrote lines written earlier in the same request, and what came in between |
| `episodes` | rework episode (see `episodes.py`) |
| `unknown_records` | transcript shape the reader did not recognise |

## Limits

- The transcript format is internal to Claude Code and changes between
  versions. A record type the reader does not know lands in `unknown_records`
  and at the top of the report; the store can be deleted and rebuilt.
- A check is recognised by its command word (`pytest`, `npm test`, `mvnw`...).
  A project whose check is a wrapper script names it with
  `PLAYBOOK_OBSERVE_VALIDATION_RE` before `derive`.
- Most checks run piped (`... | tail -5`), which hides the exit status, so a
  failure is also read from the verdict line at the end of the output.
- A file view is seen through Read and through `cat`, `sed`, `head`, `tail`
  and `grep` in Bash. A view made any other way (a script, `awk`, an editor)
  is not, so "had not read the file" is an upper bound.
- A comment is recognised by how its line opens (`#`, `//`, `*`, `--`, a
  docstring's quotes), in any language: the body of a multi-line docstring
  counts as code, so the comment figures are a lower bound.
- "Lines the model wrote" follows line numbers through later edits of the
  same file by the model; a change made outside the session is not seen.
- The episode columns are observations (where the fix landed, what had been
  read), not a verdict on why. Reading a file is not the same as having
  understood it.
