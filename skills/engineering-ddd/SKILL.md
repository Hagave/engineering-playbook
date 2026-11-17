---
name: engineering-ddd
description: "DDD patterns for entities, value objects, domain services, and use cases. Invoke when DDD is present in the project (identified by domain/entities/, private constructors, factory methods, or value objects) or when creating a new project that will use DDD. Covers entity props pattern, semantic factory methods, domain service interfaces, and use case as pure orchestrator."
version: 2.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# DDD

These rules apply only when DDD is present in the project. Before applying, verify: does the project have `domain/entities/`, private constructors, or value objects? If yes, apply. If creating a new project, ask whether DDD is wanted before scaffolding.

---

## Entity pattern

Entities use a private `props` object and a private constructor. All instantiation goes through semantic factory methods. Getters expose data. Mutations go through named domain methods — never public setters.

```pseudocode
# domain/entities/user/user.entity.<ext>
class User {
  private props

  private constructor(props)

  # --- Factory methods (semantic names, not generic "create") ---

  # Business action: registering a new user
  static register(email, password_hash) -> User:
    return new User({
      id: random_id(),             # platform-native random ID
      email,
      password_hash,
      role: member_role(),
      created_at: now(),
      updated_at: now(),
    })

  # Reconstitution from persistence — bypasses business rules intentionally
  static reconstitute(props) -> User:
    return new User(props)

  # --- Getters ---
  get id()
  get email()
  get password_hash()
  get role()
  get created_at()
  get updated_at()

  # --- Domain mutations (no public setters) ---
  promote():
    props.role = admin_role()
    props.updated_at = now()

  change_password(new_hash):
    props.password_hash = new_hash
    props.updated_at = now()
}
```

**Rules:**
- Constructor is always `private`. No exceptions.
- ID generation happens inside the factory method — never in the use case. Use the platform-native random ID facility (Node example: `crypto.randomUUID()`), never a third-party package. Native runtime utilities are permitted in domain — they are part of the runtime, not external dependencies.
- Value objects are instantiated inside the factory — the factory receives already-validated VOs or raw primitives it validates itself.
- Factory method names are semantic business actions: `register()`, `activate()`, `suspend()` — not generic `create()`.
- `reconstitute()` exists for loading from persistence. It skips business rules (e.g., no duplicate ID check) — that is intentional.
- Mutations are explicit domain methods with clear intent. Never expose setters on props.

---

## Value objects

Value objects are immutable. Validation happens at construction. Equality is structural (by value), not by reference.

```pseudocode
# domain/entities/user/email.vo.<ext>
class Email {
  private value

  private constructor(value)

  static create(raw) -> Email:
    normalized = trim(lower(raw))
    if not valid_email(normalized):
      throw InvalidEmailException(raw)
    return new Email(normalized)

  get value() -> normalized_value

  equals(other) -> boolean:
    return value == other.value
}
```

**Rules:**
- No public constructor.
- Validation is exhaustive — an invalid value object must never exist.
- Always throw a domain exception (from `domain/entities/<feature>/`) on invalid input, never return `null` or `undefined`.
- VOs are co-located with their entity in `domain/entities/<feature>/`.

---

## Domain services

Domain logic that does not belong to a single entity lives as a domain service. The interface is defined in `domain/services/`. The implementation lives in `infrastructure/`.

```pseudocode
# domain/services/password-hasher.interface.<ext>
interface PasswordHasher {
  hash(plain) -> hashed
  verify(plain, hashed) -> boolean
}
```

```pseudocode
# infrastructure/services/crypto-password-hasher.<ext>
# Hexagonal architecture: use @domain/* alias. MVC/Modular: use @/* alias. Never mix both in the same project.
class CryptoPasswordHasher implements PasswordHasher {
  hash(plain) -> crypto_library.hash(plain, 12)
  verify(plain, hashed) -> crypto_library.compare(plain, hashed)
}
```

Wire the implementation to the interface in the DI container (NestJS example in a feature module):
```typescript
{ provide: PASSWORD_HASHER, useClass: CryptoPasswordHasher }
```

**Rules:**
- Domain service interfaces live in `domain/services/` — never in `application/` or `infrastructure/`.
- The use case injects the interface token, never the concrete class.
- Implementations live in `infrastructure/` and are wired via dependency injection (NestJS example: `provide:` in modules).
- Use domain services for: hashing, encryption, external policy checks, ID generation strategies — anything that is domain logic but crosses entity boundaries or requires infrastructure.

---

## Use case — orchestration only

The use case does not contain business logic. It orchestrates: create value objects, call domain factory, persist, return output DTO.

```pseudocode
# application/usecases/user/register-user.usecase.<ext>
class RegisterUserUseCase {
  constructor(user_repository, password_hasher)

  execute(input) {
    email = Email.create(input.email)

    # validate password constraints before hashing — use static validate(), not createFromPlain()
    # createFromPlain() creates a VO holding plain text, which should never exist beyond validation
    Password.validate(input.password)

    hash = password_hasher.hash(input.password)
    user = User.register({ email, password: Password.reconstitute(hash), name: input.name })

    # save() throws a domain conflict exception if the email already exists via unique constraint.
    # No pre-check needed — avoid a read-before-write race.
    user_repository.save(user)

    return { id: user.id, email: user.email, name: user.name, created_at: user.created_at }
  }
}
```

**What belongs in the use case:**
- Creating value objects from raw input (VO factories throw on invalid input)
- Calling domain factory methods
- Calling domain service interfaces (hashing, sending email, etc.)
- Persisting via repository
- Mapping to output DTO

**What never belongs in the use case:**
- ID generation, crypto, hashing, or any infrastructure utility — these belong in the entity factory or a domain service implementation
- Business rule logic (validation of invariants, default values, role assignment) — these belong in the entity
- Calling `new User(...)` directly — always use the semantic factory
- **Read-before-write existence checks** — never call `findByEmail` / `existsByEmail` before a `save` to detect duplicates. This is a race condition: two concurrent requests can both pass the check and both succeed the write. Database unique constraints are the correct enforcement point. The repository boundary maps the constraint violation to a domain conflict exception (Prisma example: `mapPrismaError` translates `P2002` to a 409 conflict).

```pseudocode
# wrong — race condition window between check and write, two DB round-trips
exists = user_repository.exists_by_email(email)
if exists: throw UserAlreadyExistsException(email)
user_repository.save(user)

# correct — one write; unique constraint + boundary error mapping handles the conflict
user_repository.save(user)  # throws domain conflict exception on duplicate email
```

Value objects may be created in the use case before calling the factory (e.g., `Email.create(input.email)`) or inside the factory if the factory accepts raw primitives — both are valid. The rule is that business invariants live in the entity or VO, never in the use case. If the factory signature accepts a VO, the use case creates it; if it accepts a raw primitive, the factory creates the VO internally.

---

## Value object validation without VO instantiation

When a VO's validated value cannot be used directly (e.g., plain-text password that will be hashed), expose a static `validate()` method instead of relying on the caller to discard the VO:

```pseudocode
# password.vo.<ext>
class Password {
  static MIN_LENGTH = 8
  private value
  private constructor(value)

  # validate plain text without creating a VO — use before hashing
  static validate(plain) -> void:
    if length(plain) < Password.MIN_LENGTH:
      throw InvalidPasswordException("Password must be at least " + Password.MIN_LENGTH + " characters")

  # reconstitute from stored hash — bypasses validation intentionally
  static reconstitute(hash) -> Password:
    return new Password(hash)

  get value() -> value
}
```

**Why not `createFromPlain`:** a VO holding plain text is a security risk — it can be accidentally logged, serialized, or passed to the wrong layer. The only valid "plain password" VO would be one that hashes on construction, coupling the VO to a crypto operation. Avoid both patterns. Validate plain text via `validate()`, then reconstitute the VO from the hash produced by the domain service.

---

## Identifying DDD in an existing project

DDD is present when the project has any of:
- `domain/entities/` directory with entity files
- Private constructors in entity classes
- Value object files (e.g., `.vo.ts` in TypeScript projects)
- Repository interfaces separated from their implementations (in hexagonal: `domain/repositories/`; in modular/MVC the active arch-* skill owns the placement)
- `domain/services/` directory

When DDD is identified, load this skill and apply all rules above before writing or modifying any domain, application, or use case code.

---

## New project — always ask

When scaffolding a new project, always ask: **"Do you want DDD?"** before creating any domain structure.

If yes: add `domain/services/` to the scaffold and follow all patterns above.
If no: use a simpler layered structure without entity factories, props pattern, or domain services.

Never assume DDD is wanted. Never apply DDD patterns to a project that did not request them.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
