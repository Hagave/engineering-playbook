---
name: engineering-typescript
description: "**Opt-in for TypeScript:** load only when the project is TypeScript. TypeScript type safety, async patterns, and code style rules. Use whenever writing or reviewing TypeScript code: enforces no-any, explicit return types, correct async/await usage, and import order."
version: 3.1.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# TypeScript

## Type safety

- `any` is forbidden. Use `unknown` with type guards, or explicit generics.
- All public functions and class methods must declare explicit return types.
- Async functions: `Promise<T>` — never infer `Promise<any>`.
- Callbacks passed to array methods require explicit return types:
  ```typescript
  items.map((item): string => item.value)       // correct
  items.map((item) => item.value)                // wrong — return type inferred
  results.filter((r): boolean => r.ok)           // correct
  ```
- `strictNullChecks` is on. Handle `null` and `undefined` explicitly. Never use non-null assertion (`!`) without a preceding guard.
- Prefer discriminated unions over optional fields for state modeling:
  ```typescript
  // wrong
  type Result = { data?: User; error?: string };

  // correct
  type Result = { ok: true; data: User } | { ok: false; error: string };
  ```

## Async

- `async/await` over `.then()/.catch()` chains.
- Any function with `Promise<T>` in its return type must be declared `async`. A non-async function that just returns a Promise is a latent bug: if a try/catch is added later, rejected Promises will silently bypass the error handler.
- Inside `async` functions, always `return await promise` — never `return promise`. The difference is invisible in the happy path but critical inside try/catch: `return promise` exits the try/catch frame before the Promise rejects, so the catch block never runs:
  ```typescript
  // wrong — Prisma error bypasses the catch block
  async save(entity: Entity): Promise<void> {
    try {
      return this.prisma.user.update(data);
    } catch (e) {
      throw new DatabaseError(e); // never executes
    }
  }

  // correct — error is caught
  async save(entity: Entity): Promise<void> {
    try {
      return await this.prisma.user.update(data);
    } catch (e) {
      throw new DatabaseError(e); // works
    }
  }
  ```
  ESLint rule: `@typescript-eslint/return-await: "always"`.
- Never `await` inside `Array.map` — it does not parallelize and the outer await resolves immediately:
  ```typescript
  // wrong
  const results = await items.map(async (item) => process(item));

  // correct — parallel
  const results = await Promise.all(items.map((item) => process(item)));

  // correct — sequential (only when order or back-pressure matters)
  for (const item of items) { await process(item); }
  ```
- All Promises must be awaited or explicitly `void`ed. Floating promises are errors.
- Never mix callbacks with async/await in the same control flow.

## Code style

- No magic numbers or strings — declare named constants.
- No deeply nested ternaries — use early returns or named intermediate variables.
- Unused variables must be prefixed with `_`.
- No commented-out code committed to the repository.
- `process.env` access only inside config validation modules. Inject config service everywhere else.
- Import order: framework/external first, then internal aliases, then local relative — separated by blank lines.

---

## DTOs with class-validator decorators

DTO property declarations must use the `declare` keyword when the project uses `emitDecoratorMetadata` and `class-validator`.

```typescript
// wrong — TypeScript emits a property initializer: `email = undefined`
// that initializer runs after decorators, silently clearing metadata
class CreateUserDto {
  @IsEmail()
  @ApiProperty()
  email: string;
}

// correct — `declare` signals a type-only annotation; no runtime initializer is emitted
class CreateUserDto {
  @IsEmail()
  @ApiProperty()
  declare email: string;

  @IsString()
  @MinLength(8)
  @ApiProperty()
  declare password: string;
}
```

Without `declare`, `useDefineForClassFields` (TypeScript 5+ default) causes the compiler to emit a property initializer after each decorator block, which overwrites the decorator metadata and makes `class-validator` silently skip the field — validation passes even for invalid input.

---

## Path aliases

Alias choice depends on architecture — never use relative `../../` imports across module or layer boundaries.

**Hexagonal architecture** — layer-specific aliases:

```json
{
  "compilerOptions": {
    "paths": {
      "@domain/*": ["src/domain/*"],
      "@application/*": ["src/application/*"],
      "@infrastructure/*": ["src/infrastructure/*"],
      "@presentation/*": ["src/presentation/*"],
      "@config/*": ["src/config/*"],
      "@common/*": ["src/common/*"]
    }
  }
}
```

**MVC / Modular monolith** — single generic alias:

```json
{
  "compilerOptions": {
    "paths": {
      "@/*": ["./src/*"]
    }
  }
}
```

Use the aliases that match the active architecture skill. Never mix both patterns in the same project.

---

## TypeScript configuration

**Canonical strict flag set** — the strictest baseline for any project on this playbook. `engineering-refactor-tooling` audits legacy projects against this exact list:

```json
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true
  }
}
```

Reference `tsconfig.json` for NestJS projects:

```json
{
  "compilerOptions": {
    "module": "nodenext",
    "moduleResolution": "nodenext",
    "resolvePackageJsonExports": true,
    "esModuleInterop": true,
    "isolatedModules": true,
    "declaration": true,
    "removeComments": true,
    "emitDecoratorMetadata": true,
    "experimentalDecorators": true,
    "allowSyntheticDefaultImports": true,
    "target": "ES2023",
    "sourceMap": true,
    "outDir": "./dist",
    "baseUrl": "./",
    "paths": { },
    "incremental": true,
    "skipLibCheck": true,
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "strictBindCallApply": false,
    "noFallthroughCasesInSwitch": false,
    "forceConsistentCasingInFileNames": true
  }
}
```

Fill `paths` with the aliases matching the active architecture (hexagonal or generic — see section above).

`"module": "nodenext"` is required for correct ESM resolution and for `import.meta.dirname` in `eslint.config.ts`. `"isolatedModules": true` is required for SWC (Vitest) compatibility.

For linting and formatting configuration (Biome, typescript-eslint, scripts), load `engineering-linting`.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
