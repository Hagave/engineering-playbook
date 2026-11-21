---
name: engineering-scaffold
description: "BLOCK HERE. For ANY greenfield request (CRUD, API, REST, scaffold-from-scratch, 'crie um X em Y'), this skill MUST load first — BEFORE the language opt-in. Present the 13-row canonical checklist below IN ORDER. Do NOT write code, do NOT load the language opt-in (engineering-go etc.) for stack answers, and do NOT ask informal questions like 'what storage?' until all 13 rows have an answer from their owner (engineering-governance, Decision ownership): a row the request fixes is filled from it, a row it delegates you fill and record, every other row the user answers. Stack questions only — entity/field modeling comes after the stack is decided. If the language is named, filter the language-specific rows from the matching `engineering-{lang}` opt-in; if not, ask first. Default for an empty project: **none** for framework/runtime, **no** for every yes/no row. Do NOT pick Database=X + Database access=none. No informal shortcuts."
version: 3.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->

<!-- CANONICAL-CHECKLIST -->
The questions in the table below are the **only** valid pre-scaffold checklist for this skill. When this skill is loaded for a request that fits a backend scaffold:
1. THIS SKILL MUST BE LOADED FIRST. Load it BEFORE any language opt-in (engineering-go, engineering-java, etc.). It is the canonical entry point for any greenfield scaffold/CRUD/API request. If it is not loaded, the rest of the playbook cannot enforce the stack contract.
2. DO NOT START SCAFFOLDING OR ASKING INFORMAL QUESTIONS UNTIL ALL 13 ROWS HAVE AN ANSWER FROM THEIR OWNER (Decision ownership in `engineering-governance`). A row the request fixes ("in NestJS with Postgres") is filled from the request. A row the request delegates ("decide the rest yourself") you fill, marked as your decision with its reason. Every other row is the user's to answer. Showing the table with those answers is the gate: any code or shell command run while a row is still open is a violation of this skill. When no row is open, show the filled table and proceed in the same turn.
3. The 13 questions are about the STACK, not the domain. Do NOT ask about entity names, field names, or domain modeling at this stage (e.g. "what resource does the CRUD manage?" is not a stack question). The domain model is iterated after the stack is decided.
4. Detect the implementation language from the user's prompt. Load the matching language opt-in skill (e.g. `engineering-go` for Go) and use its options for the language-specific rows (Package manager, Framework / runtime, Database access). The reference table below is a fallback when no language opt-in is loaded.
5. If no language is identifiable, present the canonical full-merge table below and ask the language as the first conversational question. Block here until the user names the language.
6. Present every question in the table, in order, with the exact option sets listed. Do not skip rows. Do not add rows that are not in the table (no "Persistence", "Entity", or other invented categories — "Framework / runtime" is a canonical row before Architecture, which has the options hexagonal / modular / MVC).
7. Do not paraphrase the questions or options. The user is expected to answer in terms of the option labels exactly as written.
8. If the user request fits the playbook (backend, chosen framework/runtime, chosen architecture, persistent state), present the table as-is and stop for the rows that are still open.
9. If the user request does NOT fit (frontend-only, no backend involved), say so explicitly: "this skill does not apply — request is not a backend scaffold" — and stop. Do not silently reframe the request to fit the table.
10. If a row has a sensible default that the user is unlikely to care about (e.g. Global interceptors = no for a simple CRUD), mention the default in parentheses next to the question rather than dropping the row.
<!-- /CANONICAL-CHECKLIST -->

# ABSOLUTE DIRECTIVE — read this before anything else

STOP. If the user asked for a greenfield CRUD/API/REST/scaffold-from-scratch, you MUST:

1. Present the 13-row canonical checklist below IN ORDER, as a numbered table.
2. WAIT for the user to answer every open row: one the request neither fixed nor delegated to you (item 2 above).
3. Only THEN scaffold.

The 13 rows cover the STACK, not the domain. Do NOT ask about entity names, field names, or domain modeling. The domain model is iterated after the stack is decided.

If you skip any row, ask an informal question instead ("what storage?" / "what entity?"), or write code while a row is still open, you are violating this skill. The harness-level spec gate does not enforce the 13-row gate — only this skill does. Default for an empty project: **none** for framework/runtime, **no** for every yes/no row (DDD?, Docker?, Logger?, HTTP security?, Global interceptors?). Database and Database access must both be set — never Database=X + Database access=none.

# Project Scaffold Checklist

These questions are mandatory before scaffolding any new project, regardless of framework. Never assume defaults. Never run a generator before all questions are answered.

---

## Mandatory pre-scaffold questions

Ask all of the following in a single message:

| # | Question | Options |
|---|---|---|
| 1 | Project name | free text |
| 2 | Package manager | (language-filtered — see reference table or language opt-in) |
| 3 | Framework / runtime | (language-filtered — see reference table or language opt-in) |
| 4 | Architecture | hexagonal / modular / MVC |
| 5 | DDD? | yes / no |
| 6 | Database | PostgreSQL / MySQL / SQLite / none | (PostgreSQL accepts pgvector extension for vector similarity; docker image: pgvector/pgvector:pg17)
| 7 | Database access | (language-filtered — see reference table or language opt-in) |
| 8 | Message broker | Kafka / RabbitMQ / none |
| 9 | Cache | Redis / in-memory / none |
| 10 | Docker? | yes / no |
| 11 | Logger? | yes / no |
| 12 | HTTP security? | yes / no |
| 13 | Global interceptors? | yes / no |

### Language-specific rows — reference table

When the language opt-in skill (`engineering-{lang}`) is NOT loaded, use this fallback so the model does not surface unrelated frameworks (e.g. NestJS / Django / Spring Boot when the user said Go):

| Language | Package manager | Framework / runtime | Database access |
|---|---|---|---|
| Go | go mod | net/http / Gin / Echo / Chi / none / (other) | database/sql / sqlx / pgx / GORM / ent / none |
| Java | Maven / Gradle | Spring Boot / Quarkus / Micronaut / none / (other) | Hibernate / JPA / jOOQ / JDBC / none |
| Rust | Cargo | axum / actix-web / Rocket / none / (other) | sqlx / sea-orm / diesel / none |
| Python | pip / poetry / uv / pipenv | Django / FastAPI / Flask / none / (other) | SQLAlchemy / psycopg / asyncpg / Django ORM / none |
| C# | NuGet / dotnet CLI | ASP.NET Core / Minimal APIs / none / (other) | Entity Framework Core / Dapper / none |
| Kotlin | Gradle (Kotlin DSL) | Spring Boot / Ktor / none / (other) | Exposed / jOOQ / Hibernate / none |
| Ruby | Bundler / gem | Rails / Sinatra / Hanami / none / (other) | ActiveRecord / Sequel / none |
| TypeScript | npm / pnpm / yarn / bun | NestJS / Express / Fastify / Hono / none / (other) | Prisma / TypeORM / Drizzle / raw SQL via framework-standard driver / none |

**Priority:** the language opt-in skill\'s "Frameworks" and "Database access" sections are the source of truth. The reference table above is a fallback for when the skill is not loaded. If the two diverge, trust the live skill.

If the user has already answered some of these in their request, only ask for what is missing.

**Scope rules that flow from the answers:**
- If `Database` ≠ none, `Database access` MUST also ≠ none. PostgreSQL accepts any of the ORM options or raw SQL via a PostgreSQL driver; MySQL accepts an ORM or raw SQL via a MySQL driver; SQLite accepts an ORM or raw SQL via a SQLite driver. PostgreSQL supports the pgvector extension for vector similarity; docker image: `pgvector/pgvector:pg17` if vector is needed.
- If `Database access` = a raw SQL driver, the project uses raw SQL against a connection pool — NO entity factories, NO ORM migrations, NO ORM schema. Schema lives in raw SQL files; migrations use a migration tool appropriate to the stack. Repositories call the connection pool directly with hand-written queries.
- **Vector support is a database concern, not a Database access concern.** A raw SQL driver (e.g. `pg` for PostgreSQL in TypeScript) is just the database driver — vector similarity (via the `pgvector` extension) is a server-side feature that works the same whether you reach it through an ORM or raw SQL. If the project needs vector search, the user picks PostgreSQL + pgvector in the docker stack; the `Database access` choice is independent.
- If `Message broker` = none, the implementation has NO Kafka/RabbitMQ producer, no broker module, no broker imports, no broker docker service.
- If `Cache` = none, the implementation has NO Redis client, no cache module, no cache docker service.
- If `DDD?` = no, the implementation has NO entity factories, NO props pattern, NO domain service interfaces — ORM entities serve as the data model directly.
- If `HTTP security?` = no, NO security middleware, NO rate limiting, NO CSRF protection.
- If `Global interceptors?` = no, no trace-id, no response-transform, no exception filter middleware.
- The chosen architecture's directory tree shows what files exist when FULLY built — it is NOT a checklist of files to create for a minimal CRUD. A hexagonal project with `Message broker = none` and `Cache = none` does NOT need the full `infrastructure/broker/` and `infrastructure/cache/` trees.

After receiving answers: summarize the full stack, marking the rows you decided under a delegation, and proceed. Ask again only when an answer opens a new decision.

---

## Initialization sequence

Execute in this exact order. Never deviate:

1. **Scaffold** — use the chosen framework's standard CLI generator (e.g. `nest new <name> --package-manager <pm> --skip-git --strict` for NestJS).
2. **Cleanup** — remove generated boilerplate that is not part of the chosen architecture.
3. **Tooling** — configure lint/format and language tooling (e.g. Biome + `tsconfig.json` for TypeScript).
4. **Single dependency install** — install ALL the dependencies decided in the questions above in one command. Splitting them across several installs creates needless peer-dependency conflicts.
5. **Check installed versions** — run the ecosystem's version check (e.g. `npm list <key-packages>`) and record exact versions. Code follows the API of the installed versions, not the API documented in the skills (skills can lag behind).
6. **Create the folder structure** — create the full directory tree (see `engineering-arch-hexagonal` or the relevant architecture skill) before writing any code file.
7. **Implement in dependency order** — domain → application → infrastructure → presentation. An artifact that depends on another is created after it.
8. **Route to framework skill** — before writing any domain code, load and execute the framework-specific skill in full. This step is mandatory and must not be skipped or deferred.

| Framework / runtime | Scaffold skill |
|---|---|
| NestJS | `engineering-nestjs` |
| Django | future `engineering-django` (not shipped yet) |
| Express / Fastify / Hono / FastAPI / Spring Boot / Rails / Gin / Echo / Phoenix / none / (other) | none currently ships — no scaffold skill exists for this framework today |

Steps 1–7 are framework-agnostic setup. The framework skill owns bootstrap configuration, security, logger, interceptors, testing, path aliases, and domain structure scaffolding. No domain or application code is written before step 8 completes.

---

## DDD impact

If DDD is **yes**: load `engineering-ddd` skill before creating any domain structure. DDD is incompatible with MVC architecture — if the user selects MVC, redirect to Hexagonal instead.

If DDD is **no**: use a simpler layered structure — no entity factories, no props pattern, no domain services. ORM entities can serve as the data model directly.

---

## Docker impact

If Docker is **yes**: load `engineering-docker` skill after the framework scaffold is complete. Docker files are generated last — after the project structure, dependencies, and tooling are in place.

If Docker is **no**: skip entirely. Never generate Dockerfile or docker-compose speculatively.

---

## Logger impact

If Logger is **yes**: the framework skill will install and configure structured logging (e.g. pino for Node.js).

If Logger is **no**: use the framework's built-in logger if one exists, or leave it for the user to decide later.

---

## HTTP security impact

If HTTP security is **yes**: the framework skill will install and configure HTTP security headers, CORS, and rate limiting appropriate for the framework.

If HTTP security is **no**: no security middleware is installed. The project will have no HTTP-level protection beyond the framework defaults.

---

## Interceptors impact

If Global interceptors is **yes**: the framework skill will create trace-ID, response-transform interceptors, and a global exception filter. The user will choose which subset to include. Request/response logging is part of the structured logger choice.

If Global interceptors is **no**: skip interceptor generation entirely.

---

## Framework routing

Framework routing is **step 8** of the initialization sequence above. Do not treat it as an optional appendix — it is a numbered, blocking step. The table below is a reference only; execution happens in step 8.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
