---
name: engineering-python
description: "**Opt-in for Python:** load when Python is the implementation language. Python ecosystem (FastAPI, Django, Flask), package manager (uv, Poetry), database (SQLAlchemy, Django ORM, psycopg), test runner (pytest), linter/formatter (Ruff) and type checker (mypy, Pyright), and idiomatic patterns for layering, DDD, and concurrency in Python."
version: 1.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Python

## Runtime and package manager

- Package manager: `uv` by default; Poetry when the project already uses it
- Version manager: `uv python`, or the interpreter the project pins in `.python-version`
- Lockfile: `uv.lock` (or `poetry.lock`); dependencies are declared in `pyproject.toml`
- Build tool: the build backend named in `pyproject.toml` (`hatchling` is the common default)
- Python publishes no LTS line: use the newest stable release still in its support window, never a prerelease (Dependency versions in `engineering-governance`).

## Frameworks

| Framework | When to pick |
|---|---|
| FastAPI | Default for HTTP APIs: typed request models (Pydantic), async handlers, OpenAPI generated from the code |
| Django | Server-rendered apps or an admin-heavy product that benefits from its ORM, migrations and auth out of the box |
| Flask | A small synchronous service where FastAPI's model layer is more than it needs |

## Database access

| Layer | Options |
|---|---|
| ORM | SQLAlchemy 2 (typed `Mapped[...]` declarative), Django ORM |
| Raw SQL driver | psycopg 3, asyncpg |
| Migration tool | Alembic (with SQLAlchemy), Django migrations |

## Test runner

- Primary: `pytest`
- Test layout: `tests/` at the project root, mirroring the package tree, files named `test_*.py`
- Coverage: `pytest-cov`
- Mocking: fakes for ports first; `unittest.mock` (or `pytest-mock`) only at system boundaries
- Async tests: `pytest-asyncio` or `anyio`'s pytest plugin, whichever the project already has.

## Linting and formatting

- Linter: Ruff
- Formatter: Ruff (`ruff format`)
- Type checker: mypy in `--strict` mode, or Pyright in strict mode. No `Any` in production code: an untyped boundary value is `object` narrowed with `isinstance`, or a parsed model.

## Idiomatic patterns

### Value objects

```python
@dataclass(frozen=True, slots=True)
class Email:
    value: str

    def __post_init__(self) -> None:
        if "@" not in self.value or len(self.value) > 254:
            raise InvalidEmailError(self.value)
```

### Entities

```python
class User:
    def __init__(self, id: UUID, email: Email, status: UserStatus) -> None:
        self._id, self._email, self._status = id, email, status

    @classmethod
    def register(cls, email: Email) -> "User":
        return cls(uuid4(), email, UserStatus.PENDING)

    def activate(self) -> None:
        if self._status is not UserStatus.PENDING:
            raise InvalidTransitionError(self._status, UserStatus.ACTIVE)
        self._status = UserStatus.ACTIVE
```

Python has no private constructor: the factory classmethod is the documented way in, and the domain test suite builds entities only through it.

### Dependency injection

```python
class UserRepository(Protocol):
    async def find_by_id(self, user_id: UUID) -> User | None: ...

class GetUser:
    def __init__(self, users: UserRepository, clock: Clock) -> None:
        self._users, self._clock = users, clock
```

Wire concrete adapters in one composition root (FastAPI `Depends` at the edge, or a plain factory module); the domain and application layers never import them.

### Async / concurrency

```python
async with asyncio.TaskGroup() as group:
    tasks = [group.create_task(enrich(item)) for item in batch]
results = [task.result() for task in tasks]
```

Bound fan-out with an `asyncio.Semaphore` when the batch comes from input. A blocking call (a sync driver, CPU work) inside a coroutine runs through `asyncio.to_thread` or a process pool, never directly on the event loop.

## Layer boundaries

Layers are packages under one top-level package (`src/<app>/domain`, `application`, `infrastructure`, `presentation` in a hexagonal project). The domain package imports only the standard library and its own modules: no FastAPI, Pydantic, SQLAlchemy or Django import. Pydantic models live at the HTTP edge and map to domain objects there; SQLAlchemy models live in infrastructure and map to entities in the repository. An import-linter contract (`import-linter`) can enforce the direction when the project adopts one. Generic placement rules live in `engineering-arch-hexagonal`, `engineering-arch-modular`, and `engineering-arch-mvc`.

## Cross-references

- DDD patterns: engineering-ddd
- Layer rules: engineering-architecture
- Hexagonal structure: engineering-arch-hexagonal
- Modular monolith: engineering-arch-modular
- MVC: engineering-arch-mvc
- Concurrency: engineering-concurrency
- Error handling: engineering-error-handling
- Testing: engineering-testing
- Performance: engineering-performance

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
