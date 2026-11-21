---
name: engineering-cpp
description: "**Opt-in for C++:** load when C++ is the implementation language. C++ ecosystem (Drogon, Boost.Beast, gRPC), build and packages (CMake, vcpkg, Conan), database (libpqxx, SOCI), test runner (GoogleTest, Catch2), clang-tidy, clang-format and sanitizers, and idiomatic patterns for layering, DDD, and concurrency in modern C++."
version: 1.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# C++

## Runtime and package manager

- Package manager: vcpkg in manifest mode (`vcpkg.json`) by default; Conan when the project already uses it
- Version manager: the compiler and CMake versions the project pins (a `CMakePresets.json` toolchain, a container image)
- Lockfile: the vcpkg `builtin-baseline` in `vcpkg.json`, or `conan.lock`
- Build tool: CMake with presets; Ninja as the generator
- Language standard: C++20 at least (`set(CMAKE_CXX_STANDARD 20)`, `CMAKE_CXX_STANDARD_REQUIRED ON`, extensions off). C++23 when every compiler the project builds with supports the features it uses.

## Frameworks

| Framework | When to pick |
|---|---|
| Drogon | Default for an HTTP API: routing, filters, async database client and JSON in one mature framework |
| Boost.Beast (on Asio) | A service that needs control of the HTTP and WebSocket layer, or already depends on Boost |
| gRPC C++ | Service-to-service RPC with a protobuf contract |

## Database access

| Layer | Options |
|---|---|
| ORM | none is mainstream; map rows in the repository by hand, or use sqlpp11 for typed queries |
| Raw SQL driver | libpqxx (PostgreSQL), SOCI (several databases) |
| Migration tool | a language-agnostic tool run as its own step (Flyway, dbmate) |

## Test runner

- Primary: GoogleTest with GoogleMock; Catch2 when the project already uses it
- Test layout: `tests/` at the project root mirroring `src/`, registered with CTest (`gtest_discover_tests`)
- Coverage: `llvm-cov` or `gcov`/`lcov`
- Mocking: fakes implementing the port interface first; GoogleMock only at system boundaries
- Run the suite under AddressSanitizer and UndefinedBehaviorSanitizer in CI, and the concurrent tests under ThreadSanitizer.

## Linting and formatting

- Linter: clang-tidy (with the `bugprone-*`, `cppcoreguidelines-*`, `modernize-*` and `performance-*` checks the project enables)
- Formatter: clang-format
- Type checker: the compiler, with warnings as errors (`-Wall -Wextra -Wpedantic -Werror`, `/W4 /WX` on MSVC)

## Idiomatic patterns

### Value objects

```cpp
class Email {
public:
    static Email of(std::string value) {
        if (value.find('@') == std::string::npos || value.size() > 254) throw InvalidEmail(value);
        return Email(std::move(value));
    }
    const std::string& value() const noexcept { return value_; }
    friend bool operator==(const Email&, const Email&) = default;
private:
    explicit Email(std::string value) : value_(std::move(value)) {}
    std::string value_;
};
```

### Entities

```cpp
class User {
public:
    static User registerNew(UserId id, Email email) { return User(id, std::move(email), Status::Pending); }
    void activate() {
        if (status_ != Status::Pending) throw InvalidTransition(status_, Status::Active);
        status_ = Status::Active;
    }
private:
    User(UserId id, Email email, Status status) : id_(id), email_(std::move(email)), status_(status) {}
    UserId id_; Email email_; Status status_;
};
```

### Dependency injection

```cpp
class UserRepository {
public:
    virtual ~UserRepository() = default;
    virtual std::optional<User> findById(const UserId& id) = 0;
};

class GetUser {
public:
    GetUser(UserRepository& users, const Clock& clock) : users_(users), clock_(clock) {}
private:
    UserRepository& users_; const Clock& clock_;
};
```

Own resources with RAII: `std::unique_ptr` for sole ownership, references for borrowed collaborators that outlive the user, no raw `new`/`delete`. Wire concrete adapters in `main` (the composition root).

### Async / concurrency

```cpp
std::vector<std::jthread> workers;
for (auto& shard : shards)
    workers.emplace_back([&shard](std::stop_token stop) { process(shard, stop); });
// jthread joins on destruction; stop_token carries cancellation
```

Shared mutable state is guarded by a `std::mutex` with `std::scoped_lock`, or avoided by message passing. For I/O-bound services, use the framework's event loop (Asio coroutines, Drogon's async API) instead of a thread per request.

## Layer boundaries

Each layer is its own CMake library target (`domain`, `application`, `infrastructure`, `presentation`), and `target_link_libraries` is the dependency direction: `domain` links nothing of the project's and no framework, `application` links `domain`, adapters link both. A layer that includes a header it does not link fails to build, so the direction is checked by the build. Headers of a target that other targets must not see stay under its private include directory. Generic placement rules live in `engineering-arch-hexagonal`, `engineering-arch-modular`, and `engineering-arch-mvc`.

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
