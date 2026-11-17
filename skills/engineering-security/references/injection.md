# Injection — SQL, NoSQL, OS command, code, template

CWE-89, CWE-943, CWE-78, CWE-88, CWE-94, CWE-1336

**Load when** the code builds a query, a shell command, a template, or evaluates code, and any part of it can come from outside the process.

## Invariant

Input fills data slots. It never changes the structure of a query, a command, a template or a program.

## Sinks

- SQL built as a string: concatenation or template literals passed to `query()`, `$queryRawUnsafe`, `$executeRawUnsafe`, `knex.raw`, `sequelize.query`, `cursor.execute(f"...")`.
- NoSQL filters taken from the request: `find(req.body)`, `findOne({ email: body.email })` where `body.email` can be an object, `$where`, `$expr` and `$function` built from input.
- Shell: `exec`, `execSync`, `spawn(..., { shell: true })`, `os.system`, `subprocess.*(..., shell=True)`, backticks in Ruby, `Runtime.exec` with one string.
- Arguments: user input placed where a program reads options (`git clone <input>`, `ssh <input>`, `tar <input>`).
- Code: `eval`, `new Function`, `vm.runIn*Context`, `setTimeout(string)`, dynamic `import()`/`require()` of an input-derived path.
- Templates compiled from input: `Handlebars.compile(input)`, `ejs.render(input)`, `nunjucks.renderString(input)`, `Template(input)` in Jinja2, `pug.render(input)`.

## Control

- Parameterized queries or the ORM's bound parameters. Identifiers (column, table, sort field) come from an allowlist, never from input.
- Scalars are checked as scalars before they reach a NoSQL filter: reject an object where a string is expected, and reject keys starting with `$` or containing `.`.
- No shell. Use `execFile` / `spawn(cmd, argsArray)` / `subprocess.run([...])`. Put `--` before user-supplied arguments, and allowlist values that are options.
- Never evaluate input as code. If a feature seems to need it, it needs a parser for a restricted language instead.
- Templates are fixed at build time. Input reaches them only as context data, rendered with autoescaping.

## Negative proof

Each row is a test that sends the input and asserts the expected result, and that the side effect did not happen.

| Hostile input | Where | Expected |
|---|---|---|
| `' OR '1'='1` | login or search parameter | no extra rows; login fails |
| `1; DROP TABLE users--` | numeric id | 400 (not a number); table intact |
| `{"password": {"$ne": null}}` | JSON body, NoSQL login | 400 (password must be a string) |
| `{"$where": "sleep(1000)"}` | filter object | 400 |
| `report.pdf; id` | filename passed to a command | treated as one literal name, or rejected |
| `$(id)` / `` `id` `` | any value reaching a command | literal, or rejected |
| `--upload-pack=touch /tmp/x` | argument to `git` | rejected (option-shaped value) |
| `{{7*7}}` / `${7*7}` / `<%= 7*7 %>` | value rendered by a template | output contains the literal text, never `49` |

## Dynamic proof

Not provable in the lab: the passive baseline sends no payloads. It does report database errors and stack traces in the responses it crawls (Application Error Disclosure), worth tracing to their sink. The negative tests above are the proof.

## Review signal

- String interpolation or concatenation anywhere between the input and the sink.
- `shell: true`, `shell=True`, `Unsafe` or `raw` in the API name, `eval`.
- A request body passed whole into a query filter.
- A template string that is not a literal.
