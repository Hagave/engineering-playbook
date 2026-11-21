---
name: engineering-design-patterns
description: "SOLID principles, Clean Code naming conventions, and design patterns. Invoke when SOLID violations, naming ambiguity, or design pattern decisions arise during implementation or review."
version: 2.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Design Patterns & Naming

---

## SOLID

### S — Single Responsibility
One class, one reason to change. A class that changes for two different reasons has two responsibilities — split it.

```pseudocode
# wrong — handles both business logic and formatting
class UserService {
  create_user(input) {
    user = repo.save(input)
    return format("User " + user.name + " created on " + user.created_at)  # formatting here
  }
}

# correct — formatting extracted
class UserService {
  create_user(input) -> User { ... }
}
class UserFormatter {
  to_creation_message(user) -> string { ... }
}
```

Signal: class exceeds ~200 lines or has more than one type of collaborator (e.g., a repository AND an email sender AND a formatter).

### O — Open/Closed
Add behavior via new classes, not by modifying existing ones.

```pseudocode
# wrong — adding a new payment type requires modifying this class
class PaymentProcessor {
  process(type, amount) {
    if type == "stripe": { ... }
    if type == "paypal": { ... }
  }
}

# correct — new payment types extend without touching existing code
interface PaymentGateway {
  process(amount) -> void
}
class StripeGateway implements PaymentGateway { ... }
class PaypalGateway implements PaymentGateway { ... }
```

### L — Liskov Substitution
A subclass must work wherever the parent is expected — without the caller knowing the difference.

Signal: a subclass throws an exception for a method the parent supports, or returns a narrower type than declared.

### I — Interface Segregation
Prefer focused interfaces with 2–4 methods over wide ones with 10+.

```pseudocode
# wrong — implementations forced to implement irrelevant methods
interface UserRepository {
  find_by_id(id) -> User
  save(user) -> void
  generate_report() -> bytes  # not data access
}

# correct — separate concerns
interface UserRepository { find_by_id(id) -> User; save(user) -> void }
interface UserReportGenerator { generate_report() -> bytes }
```

### D — Dependency Inversion
Depend on abstractions (interfaces), not concretions (classes).

```pseudocode
# wrong — depends on concrete repository
class CreateUserUseCase {
  constructor(repository: ConcreteUserRepository)
}

# correct — depends on the abstraction
class CreateUserUseCase {
  constructor(repository: UserRepository)  # injected by the framework
}
```

---

## Clean Code naming

### Functions and methods
Name = verb + noun that describes the action and its subject. The name must answer "what does this do?" without reading the body.

| Wrong | Right |
|---|---|
| `process()` | `processPayment()` |
| `handle()` | `handleUserCreated()` |
| `doStuff()` | — (never acceptable) |
| `getData()` | `findUserById()` |
| `check()` | `validateEmailFormat()` |
| `update()` | `updateUserRole()` |

### Classes
Noun that describes what the class IS — not what it does.

| Wrong | Right |
|---|---|
| `UserManager` | `UserService` |
| `DataHandler` | `UserRepository` |
| `Processor` | `PaymentProcessor` |
| `Helper` | name what it helps with: `DateFormatter` |

Avoid: `Manager`, `Handler`, `Processor`, `Helper`, `Utility`, `Data`, `Info` as standalone suffixes with no context.

### Booleans
Always a question: `is`, `has`, `can`, `should`, `was` prefix.

```pseudocode
# wrong
active = user.status == "active"
permission = roles.includes("admin")

# correct
is_active = user.status == "active"
has_admin_permission = roles.includes("admin")
can_delete_post = user.role == "admin" or post.author_id == user.id
```

Never double negatives: `isNotInactive`, `hasNoErrors`. Invert to positive form.

### Variables and parameters
Named after what they CONTAIN, not their type.

```pseudocode
# wrong
data = user_repository.find_by_id(id)
list = user_repository.find_all()
obj = { id: user.id, email: user.email }

# correct
user = user_repository.find_by_id(id)
users = user_repository.find_all()
create_user_payload = { id: user.id, email: user.email }
```

### No abbreviations
Use full words unless the abbreviation is universally known in the domain (`HTTP`, `DTO`, `ID`, `URL`, `API`, `DB`).

```pseudocode
# wrong
usr = repo.find_by_id(id)
cfg = new AppConfig()
fn = (callback) -> callback()

# correct
user = user_repository.find_by_id(id)
config = new AppConfig()
execute = (callback) -> callback()
```

### Constants
`UPPER_SNAKE_CASE` for module-level constants. Descriptive enough to not need a comment.

```pseudocode
MAX_LOGIN_ATTEMPTS = 5
DEFAULT_SESSION_TTL_SECONDS = 86400
USER_REPOSITORY = "USER_REPOSITORY"  # dependency injection token
```

---

## Common patterns

### Strategy
Use when behavior varies by context and adding new variants should not require modifying existing code.

```pseudocode
interface SortStrategy {
  sort(items) -> items
}
class AlphabeticalSort implements SortStrategy { ... }
class DateSort implements SortStrategy { ... }
```

When to apply: `if/else` or `switch` chains that grow with new types. Each branch is a candidate strategy.

### Cross-cutting concerns
Use middleware, guards, interceptors, pipes, or the framework equivalent for cross-cutting concerns — authentication, logging, transformation, rate limiting — not inside services or use cases. (NestJS example: guards, interceptors, and pipes are decorators.)

### Observer / Event-driven
Use when a state change in one module should trigger reactions in others, without coupling them.
Use the language/framework's in-process event facility (NestJS example: `EventEmitter2`) and Kafka/RabbitMQ or equivalent for cross-service events.

Never call another module's service directly for side effects — emit an event instead.

### Singleton
Framework services are singletons by default (NestJS example: `DEFAULT` scope). Never accumulate mutable state in instance variables — it persists across requests and causes race conditions in concurrent environments.

```pseudocode
# wrong — mutable singleton state
class ReportService {
  results = []  # shared across all requests

  generate() {
    results.push(build_report())  # race condition
    return results
  }
}

# correct — stateless
class ReportService {
  generate() {
    return [build_report()]
  }
}
```

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
