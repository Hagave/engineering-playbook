---
name: engineering-kotlin
description: "**Opt-in for Kotlin:** load when Kotlin is the implementation language. Kotlin/JVM ecosystem (Spring Boot, Ktor), Kotlin-specific idioms (data classes, sealed classes, coroutines, null safety), build tool (Gradle Kotlin DSL), and idiomatic patterns for layering, DDD, and concurrency in Kotlin."
version: 1.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->

# Kotlin

## Runtime and package manager

- Package manager: Gradle with Kotlin DSL (`build.gradle.kts`)
- Version manager: `sdkman`, optional
- Lockfile: Gradle dependency locking writes `gradle.lockfile`
- Build tool: Gradle (Kotlin DSL)

## Frameworks

| Framework | When to pick |
|---|---|
| Spring Boot | Dominant production backend; mature JVM ecosystem, Spring Data JPA/Hibernate integration |
| Ktor | JetBrains' coroutine-native async framework; lightweight services and APIs |
| Micronaut | Compile-time DI, fast startup, Kotlin-friendly |

## Database access

| Layer | Options |
|---|---|
| ORM | Exposed, jOOQ, Hibernate (with Kotlin) |
| Raw SQL driver | `java.sql` (JDBC interop), `postgresql-async`, Ktorm |
| Migration tool | Flyway, Liquibase |

## Test runner

- Primary: JUnit 5 (Jupiter) with `kotlin.test` assertions
- Test layout: `src/test/kotlin/...` mirroring `src/main/kotlin/...`
- Coverage: Kover (Kotlin-native) or JaCoCo
- Mocking: MockK (Kotlin-native, idiomatic)

## Linting and formatting

- Linter: ktlint (Kotlin-native), detekt (static analysis)
- Formatter: ktlint built-in formatter
- Type checker: Kotlin compiler

## Idiomatic patterns

#### Value objects

Use a `data class` for immutable values and a `sealed interface` for closed value/state sets:

```kotlin
data class Email(val value: String) {
    init {
        require(value.isNotBlank()) { "email must not be blank" }
    }
}

sealed interface UserState {
    data object Active : UserState
    data class Suspended(val reason: String) : UserState
}
```

#### Entities

Use a private constructor plus a semantic factory; `data class` only when JPA mapping does not conflict:

```kotlin
class User private constructor(
    val id: UserId,
    val email: Email,
) {
    companion object {
        fun create(email: Email): User = User(UserId.random(), email)
    }
}
```

#### Dependency injection

Prefer constructor injection with `val` properties; use Koin only when the framework has no DI built in:

```kotlin
@Service
class CreateUserUseCase(private val users: UserRepository)
```

#### Async / concurrency

Use `suspend fun` and `Flow<T>`; avoid `CompletableFuture` outside Java interop:

```kotlin
suspend fun loadUser(id: UserId): User? =
    users.findById(id)

fun observeUsers(): Flow<User> =
    users.stream()
```

## Layer boundaries

Kotlin and Java share Spring Boot and the JVM; this skill owns Kotlin-specific placement notes (data classes for DTOs/value objects, sealed classes for result/state models, nullable types only where absence is valid, `suspend` functions for ports, `Flow` for streams) and delegates JVM-shared patterns such as JPA repository placement to `engineering-java`. Structure, dependency rules, and folder placement are owned by `engineering-arch-hexagonal`, `engineering-arch-modular`, and `engineering-arch-mvc`.

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
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
