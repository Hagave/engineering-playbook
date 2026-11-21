---
name: engineering-testing
description: "Testing rules: test types, what to test, coverage requirements, test doubles, and reusable test data factories. Use when writing unit tests, integration tests, or E2E tests, or when reviewing test files."
version: 2.2.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Testing

## Test file location

Tests live in a dedicated `test/` directory at the project root, organized by layer and mirroring the production source tree. The tree below is a TypeScript example — keep the structure and apply your language's own file naming and conventions (`_test.go`, `*Test.java`, `test_*.py`, `*_spec.rb`). Where a language's ecosystem mandates a different location (Go's package-local `_test.go`, Rust's `tests/` plus `#[cfg(test)]`), follow the ecosystem. Never co-locate test files inside production source (TypeScript/Vitest example: `*.spec.ts` inside `src/`).

```
test/
├── domain/
│   └── factory/
│       └── user.factory.ts          # shared fixtures, reused by every test type
├── unit/
│   ├── domain/
│   │   └── entities/
│   │       └── user/
│   │           ├── user.entity.spec.ts
│   │           └── email.vo.spec.ts
│   └── application/
│       └── usecases/
│           └── user/
│               └── register-user.usecase.spec.ts
├── integration/
│   └── repositories/
│       └── prisma-user.repository.spec.ts
└── e2e/
    └── user.e2e-spec.ts
```

Co-locating tests inside `src/` causes three problems: production build tools can accidentally include test files, the `include: ['src/**']` coverage config must then be negated with exclusions, and the source tree becomes harder to scan.

---

## Test runner

Use the project's chosen test runner consistently. In TypeScript projects, **Vitest** is the default. Do not suggest Jest unless the project already uses it.

### TypeScript/Vitest exception

For NestJS projects, SWC is required — TypeScript decorators (`@Injectable`, `@Inject`, etc.) need `emitDecoratorMetadata` which Vitest's native transformer does not support. For non-NestJS projects without decorators, the SWC plugin is optional.

```bash
<pm> add -D vitest @vitest/coverage-v8 unplugin-swc @swc/core
```

**NestJS CLI installs Jest by default.** Replace it immediately after scaffolding, before writing any test:

```bash
npm uninstall jest @types/jest ts-jest jest-mock-extended
npm install -D vitest @vitest/coverage-v8 unplugin-swc @swc/core
```

Remove `jest` key from `package.json` and the `jest.config.*` file if generated. Update `test` scripts in `package.json` to use `vitest`.

Use `vi` for mocking in Vitest. `describe`, `it`, `expect`, `beforeEach`, `afterEach` are available globally without imports.

---

## Test types

| Type | Scope | Mocking rule |
|---|---|---|
| Unit | Single class or function in isolation | Mock only at external I/O boundaries |
| Integration | Module with real dependencies (DB, Redis, message broker) | No mocks for the system under test |
| E2E | Full stack: HTTP → DB → response | Real server, real DB, real I/O |

---

## Unit test rules

- Test observable behavior, not internal implementation.
- One test verifies one behavior.
- Structure: Arrange → Act → Assert.
- Test names describe the scenario: `should return 404 when user does not exist`.
- Mock only at system boundaries (external HTTP, file system, clock, random).
- Use real DI when the framework makes it cheap.

## Integration test rules

- Use real database and cache connections, not fakes.
- Seed only the data the test requires. Clean up after each test.
- Test the full module path from repository to domain entity.

---

## In-memory testing (unit tests only)

For unit tests, prefer in-memory implementations over mocks for repositories and stateful dependencies. Mocks verify calls; in-memory fakes verify behavior.

Integration tests must use real database and cache connections — in-memory fakes are not acceptable there.

**In-memory repository pattern:**

```pseudocode
class InMemoryUserRepository {
  records = map()

  findById(id) { return records.get(id) }
  save(user) { records.set(user.id, user) }
  findAll() { return records.values() }
  clear() { records.clear() }  # Test helper — not part of the interface
}
```

Reset state between tests — direct instantiation:

```pseudocode
repo = new InMemoryUserRepository()
before_each(repo.clear)
```

**With a DI test container (NestJS example: Testing Module)** — the container binds `InMemoryUserRepository` to its interface token. Retrieve the concrete instance by class to retain access to test helpers like `clear()`:

```typescript
import { Test } from '@nestjs/testing';

let repo: InMemoryUserRepository;

beforeAll(async () => {
  const module = await Test.createTestingModule({
    providers: [
      CreateUserUseCase,
      { provide: USER_REPOSITORY, useClass: InMemoryUserRepository },
    ],
  }).compile();

// Retrieve the concrete class — not the interface token — to access clear()
  repo = module.get(InMemoryUserRepository);
});

beforeEach(() => repo.clear());
```

Never retrieve via the interface token (`module.get<IUserRepository>(USER_REPOSITORY)`) for test helpers — the return type is the interface, which does not expose `clear()`, requiring an unsafe cast.

**SQLite in-memory for integration tests** (TypeScript/TypeORM example):

```typescript
// TypeORM only — use :memory: datasource for isolation when ORM behavior must be tested
DataSource({ type: 'sqlite', database: ':memory:', synchronize: true, entities: [...] })
```

For ORMs without a compatible in-memory mode (e.g., Prisma), SQLite-in-memory is not available. Use a real PostgreSQL test instance (Docker Compose `test` profile with a dedicated test database). For tests that must match production semantics exactly (e.g., PostgreSQL-specific JSON operators, CTEs, full-text search), a real test database is mandatory regardless of ORM.

---

## What to test

- All public methods of domain entities — verify invariant enforcement and state transitions.
- Use case: happy path + each distinct error path.
- Input validation on DTOs — test that invalid input is rejected.
- Mapper transformations — test that all fields map correctly.
- Repository methods — test actual queries against a real database.

## Security controls: negative tests

A security control is proven by the attack it stops. Every control added or changed ships with negative tests: a validator, guard, authorization check, sanitizer, rate limit, parser setting, upload check, redirect check or outbound-request policy.

- Take the hostile inputs from the **Negative proof** table of the matching fiche in `engineering-security/references/`. Each row becomes one test case; a table-driven test is the usual form.
- Assert two things: the rejection (status, exception, `null`), **and** the absence of the effect. The row is unchanged in the database, no outbound request was made, no file was written, the prototype is clean, the balance is not negative. A test that only checks the status code passes while the side effect still happens.
- Test at the layer that owns the control:
  - a value object or validator: unit test;
  - a guard, middleware, authorization rule or parser configuration: integration test through the real HTTP or resolver layer;
  - a race: parallel requests against the real database.
- Hostile inputs live as named fixtures in the test data factories (`hostile.pathTraversal`, `hostile.ssrfTargets`), not as magic strings scattered across tests.
- A security finding fixed after review gets its negative test in the same change: the test fails before the fix and passes after.

## What not to test

- Private methods directly — test through the public interface.
- Framework plumbing (DI wiring, ORM connection setup) — covered by integration tests.
- Trivial getters and setters with no logic.
- Third-party library behavior.

---

## Coverage

- Domain entities and value objects: 100% branch coverage.
- Use cases: happy path + all throw paths covered.
- Utilities: all branches.
- Coverage is a floor, not a goal. A test that does not assert meaningfully contributes nothing.

### TypeScript/Vitest projects

Reference `vitest.config.ts` for NestJS (includes SWC for decorators and coverage thresholds):

```bash
<pm> add -D vite-tsconfig-paths
```

`vite-tsconfig-paths` must be installed and added to the plugins. Vite's module resolver is independent of SWC's transpiler — `unplugin-swc` handles TypeScript compilation but does not configure Vite's resolver. Without this plugin, every test that imports from a path alias (`@domain/*`, `@application/*`, etc.) fails at module resolution, even if `tsc --noEmit` passes.

```typescript
import swc from 'unplugin-swc';
import tsconfigPaths from 'vite-tsconfig-paths';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    root: './',
    coverage: {
      provider: 'v8',
      include: ['src/**'],
      thresholds: {
        lines: 80,
        branches: 80,
        functions: 80,
        statements: 80,
      },
    },
  },
  plugins: [tsconfigPaths(), swc.vite({ module: { type: 'nodenext' } })],
});
```

Without `thresholds`, `vitest run --coverage` reports but never fails. Vitest enforces only project-wide thresholds — the 100% branch coverage requirement for domain entities/VOs is enforced via code review.

---

## Test doubles

| Double | Use when |
|---|---|
| **Stub** | Providing fixed return data on read paths |
| **Mock** | Verifying that a specific call was made (the call itself is the behavior) |
| **Spy** | Wrapping real implementation to observe calls — use sparingly |
| **Fake** | Simplified real implementation (e.g., in-memory repository) — prefer over mocks for complex I/O |

Prefer fakes over mocks for repositories. Mocks verify interaction; fakes verify behavior.

---

## Test data factories

A module usually needs several test types (unit, integration, e2e), and the input data
repeats across them. A **factory** is a single reusable builder for a domain object, so
the same mocked structure is never recreated per test — no duplicated fixtures spread
across the suite. Factories are orthogonal to test doubles: a factory *builds the values*;
a double (stub/mock/fake) *replaces a collaborator*. A test typically uses both.

Factories live in `test/<layer>/factory/` (domain objects in `test/domain/factory/`) and
are imported by unit, integration, and e2e tests alike.

```pseudocode
# test/domain/factory/user_factory.<ext>
function makeUser(overrides = {}) {
  return User.create({
    name: "John Doe",
    email: "john@example.com",
    ...overrides,          # each test overrides only the fields it asserts on
  })
}
```

Rules:
- Valid defaults + partial `overrides`. A test must never depend on a default value it did not declare — if it asserts on a field, it sets that field.
- Build through the domain factory method (`User.create`), never `new`. The fixture must be the same invariant-checked object production builds; a factory that bypasses domain rules produces states that cannot occur at runtime.
- Uniqueness (emails, ids) comes from a fake-data generator (e.g. Faker), not a shared mutable counter — parallel tests must not collide on generated values.
- A plain function with `overrides` is the default. Reach for a chained builder only when construction has many conditional variations.
- Factories construct data only — no assertions, no test doubles inside them.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
