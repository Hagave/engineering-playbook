---
name: engineering-rust
description: "**Opt-in for Rust:** load when Rust is the implementation language. Cargo workspace, framework (axum, actix-web, rocket), database (sqlx, sea-orm, diesel), test runner (cargo test), clippy and rustfmt, and idiomatic patterns for layering, DDD, and concurrency in Rust."
version: 1.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Rust

## 1. Runtime and package manager

- Package manager: Cargo
- Version manager: rustup
- Lockfile: Cargo.lock
- Build tool: Cargo (`cargo build`)

## 2. Frameworks

| Framework | When to pick |
|---|---|
| axum | Default for async HTTP services; built on tokio and tower with ergonomic type-safe routing |
| actix-web | Mature high-throughput actor-style applications |
| rocket | Opinionated ergonomic APIs; useful when moving from sync-style code |

## 3. Database access

| Layer | Options |
|---|---|
| ORM | sea-orm (async), diesel (sync) |
| Raw SQL driver | sqlx (async, compile-time checked), postgres (sync), mysql (sync) |
| Migration tool | sqlx-cli, refinery |

## 4. Test runner

- Primary: `cargo test`
- Test layout: `#[cfg(test)]` modules in source files, or `tests/` for integration tests
- Coverage: `cargo llvm-cov` or `cargo tarpaulin`
- Mocking: trait-based; `mockall` for derived mocks, or hand-rolled test doubles

## 5. Linting and formatting

- Linter: Clippy (`cargo clippy`) — mandatory
- Formatter: rustfmt (`cargo fmt`) — mandatory
- Type checker: built into `cargo build`

## 6. Idiomatic patterns

#### Value objects

Use a newtype with a validating constructor and `AsRef`/`Deref` for controlled access.

```rust
pub struct Email(String);

impl Email {
    pub fn new(value: String) -> Result<Self, InvalidEmail> {
        if !value.contains('@') { return Err(InvalidEmail); }
        Ok(Self(value))
    }
}

impl AsRef<str> for Email {
    fn as_ref(&self) -> &str { &self.0 }
}
```

#### Entities

Keep fields private; expose semantic constructors and behavior through `impl`.

```rust
pub struct User {
    id: UserId,
    email: Email,
    active: bool,
}
impl User {
    pub fn new(id: UserId, email: Email) -> Self {
        Self { id, email, active: true }
    }
    pub fn deactivate(&mut self) {
        self.active = false;
    }
}
```

#### Dependency injection

Use traits as ports and accept implementations in constructors. No DI container.

```rust
pub struct CreateUser<R: UserRepository> { users: R }

impl<R: UserRepository> CreateUser<R> {
    pub fn new(users: R) -> Self { Self { users } }
}
```

#### Async / concurrency

Use tokio for async runtimes and tasks, and propagate errors with `Result`.

```rust
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let handle = tokio::spawn(async { process().await });
    handle.await??;
    Ok(())
}
```

## 7. Layer boundaries

Rust's crate and module boundaries map cleanly onto the supported architectures. Domain ports are traits, infrastructure adapters implement those traits, and application use cases receive implementations through constructors (`impl Trait`, `Arc<dyn Trait>`, or a composition root). Value objects are newtypes in `domain/`, HTTP DTOs and mappers stay in the presentation layer, and persistence mappers translate between domain entities and database models. Restrict layer-facing modules with `pub(crate)` so imports cannot cross architecture boundaries. Generic directory structure and dependency rules live in `engineering-architecture` and the selected architecture skill.

## 8. Cross-references

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
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
