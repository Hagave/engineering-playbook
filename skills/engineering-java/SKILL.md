---
name: engineering-java
description: "**Opt-in for Java:** load when Java is the implementation language. JVM ecosystem (Spring Boot, Quarkus, Micronaut), build tool (Maven / Gradle), ORM (Hibernate / JPA, jOOQ), test runner (JUnit 5), and idiomatic patterns for layering, DDD, and concurrency in the JVM ecosystem."
version: 1.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Java

## Runtime and package manager

- Package manager: Maven (declared in `pom.xml`) or Gradle (declared in `build.gradle` / `build.gradle.kts`)
- Version manager: `sdkman`, optional
- Lockfile: Maven has no lockfile; Gradle dependency locking writes `gradle.lockfile`
- Build tool: Maven (default) or Gradle

## Frameworks

| Framework | When to pick |
|---|---|
| Spring Boot | Dominant production backend; mature ecosystem, REST, Spring Data JPA/Hibernate integration |
| Quarkus | Lightweight startup, Kubernetes-native, suited to serverless and cloud workloads |
| Micronaut | Compile-time DI, fast startup, less reflection than Spring |

## Database access

| Layer | Options |
|---|---|
| ORM | Hibernate (via Spring Data JPA), jOOQ, MyBatis |
| Raw SQL driver | JDBC (standard), HikariCP (connection pool) |
| Migration tool | Flyway, Liquibase |

## Test runner

- Primary: JUnit 5 (Jupiter)
- Test layout: `src/test/java/...` mirroring `src/main/java/...`
- Coverage: JaCoCo
- Mocking: Mockito

## Linting and formatting

- Linter: Checkstyle (style), SpotBugs (static analysis), plus Maven Enforcer or Gradle built-in lint for build rules
- Formatter: Spotless or google-java-format
- Type checker: N/A — `javac` compile errors through the build tool

## Idiomatic patterns

#### Value objects

Use a Java 17 `record` for immutable values with automatic `equals`, `hashCode`, and `toString`:

```java
public record Email(String value) {
    public Email {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("email must not be blank");
        }
    }
}
```

#### Entities

Keep JPA entities behavior-rich with a private constructor plus static factory. Do not expose public setters:

```java
@Entity
@Table(name = "users")
public class User {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String email;

    private User() {}

    private User(String email) {
        this.email = email;
    }

    public static User create(String email) {
        return new User(email);
    }
}
```

#### Dependency injection

Prefer Spring constructor injection with final fields; never field injection:

```java
@Service
public class CreateUserUseCase {
    private final UserRepository users;

    public CreateUserUseCase(UserRepository users) {
        this.users = users;
    }
}
```

#### Async / concurrency

Use `CompletableFuture` or Spring `@Async`, never a raw `Thread`:

```java
CompletableFuture<User> future =
    CompletableFuture.supplyAsync(() -> users.findById(id));

future.exceptionally(error -> {
    throw new UserLoadException(error);
});
```

## Layer boundaries

Put layers in Maven/Gradle modules or packages: `domain` stays framework-free and owns entities/value objects, `application` orchestrates use cases, `infrastructure` implements ports with JPA repositories or JDBC clients, and `presentation` holds controllers and request/response DTOs. Use Java interfaces for ports and records for DTOs/value objects. Structure, dependency rules, and folder placement are owned by `engineering-arch-hexagonal`, `engineering-arch-modular`, and `engineering-arch-mvc`; this section only adds Java placement notes.

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
