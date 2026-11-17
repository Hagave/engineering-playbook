---
name: engineering-nestjs
description: "**Opt-in for NestJS:** load only when NestJS is the chosen framework. NestJS-specific rules: module structure, controllers, dependency injection, DTOs, configuration, and logging. Use when working on a NestJS project: creating modules, controllers, services, guards, interceptors, or configuring the framework."
version: 2.1.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->

# NestJS

## Project scaffolding

Never create framework files manually — use the CLI. Initialize and maintain this progress table. Update each row to ✓ as it completes. Do not advance to step N+1 without declaring step N complete with evidence.

| Step | Description | Status |
|---|---|---|
| 1 | Confirm NestJS-specific inputs | — |
| 2 | Scaffold via CLI | — |
| 3 | Remove unused boilerplate | — |
| 4 | Install project dependencies | — |
| 5 | Replace Prettier with Biome; configure typescript-eslint | — |
| 6 | Security and bootstrap setup | — |
| 7 | Logger setup | — |
| 8 | Global interceptors and exception filter | — |
| 9 | Install and configure Vitest | — |
| 10 | Configure path alias | — |
| 11 | Update package.json scripts | — |
| 12 | Scaffold domain structure | — |
| 13 | Docker setup | — |

---

**Step 1 — Ask for missing NestJS-specific inputs**

General pre-scaffold questions are handled by `engineering-scaffold`. Invoke it first if not yet answered.

| Question | Options |
|---|---|
| HTTP provider | Express (default) / Fastify |
| Architecture | MVC / Modular monolith / Hexagonal |
| Logger | nestjs-pino / NestJS built-in |
| HTTP security | helmet + CORS + throttler / none |
| Interceptors | trace-id / response-transform / exception-filter (multi-select; none to skip all) |

If the user selected "no" for any option in the pre-scaffold checklist, skip the corresponding question. If DDD is yes: load `engineering-ddd` before scaffolding domain structure.

**Step 2 — Scaffold via CLI**

```bash
nest new <project-name> --package-manager <pm> --strict
cd <project-name>
```

`nest new --strict` injects individual strict flags (`strictNullChecks`, `noImplicitAny`, etc.) into `tsconfig.json` instead of the umbrella `"strict": true`. Immediately after scaffolding, replace those individual flags with the single entry:

```json
// tsconfig.json — replace any individual strictNullChecks / noImplicitAny / etc. with:
{
  "compilerOptions": {
    "strict": true
  }
}
```

`"strict": true` enables all strict mode flags including `strictFunctionTypes`, `strictPropertyInitialization`, and `useUnknownInCatchVariables` — without it, `catch (e)` types `e` as `any`, bypassing the no-any rule entirely.

If Fastify was chosen:
```bash
<pm> add @nestjs/platform-fastify fastify
<pm> remove @nestjs/platform-express @types/express
```

> `fastify` must be a **direct** dependency, not just a transitive one from `@nestjs/platform-fastify`. With `moduleResolution: nodenext`, TypeScript only resolves types from packages listed in your own `package.json` — transitive packages are invisible to the type resolver. Without `fastify` as a direct dep, imports of `FastifyRequest`, `FastifyReply`, etc. will fail at type-check time.

Update `main.ts` (Fastify adapter — **this entire file is replaced in Step 6** by the production bootstrap from `engineering-nestjs-security`):
```typescript
import { NestFactory } from '@nestjs/core';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';
import { AppModule } from './app.module'; // Hexagonal: './presentation/modules/app.module'

async function bootstrap(): Promise<void> {
  const app = await NestFactory.create<NestFastifyApplication>(AppModule, new FastifyAdapter());
  await app.listen(3000, '0.0.0.0'); // placeholder — Step 6 replaces this with ConfigService
}

void bootstrap();
```

Never read `process.env` directly in `main.ts`. Step 6 introduces `ConfigService` — use `config.get<number>('PORT', 3000)` there.

**Step 3 — Remove unused default boilerplate and configure path aliases**

```bash
rm src/app.controller.ts src/app.controller.spec.ts src/app.service.ts
```

Remove deleted files from `app.module.ts` (controllers and providers arrays).

**Configure `tsconfig.json` path aliases now — before any source file is created.** Load `engineering-typescript`, section 'Path aliases', and apply the aliases for the active architecture. Run `tsc --noEmit` to confirm they resolve without errors.

> Path aliases must be in place before Steps 6–8, which create files that import from other layers. Files created before aliases are configured will use relative paths, which cannot be auto-corrected by the linter and require manual updates. Step 10 is a reminder only — the actual work happens here.

**Step 4 — Install project dependencies**

```bash
# Config (always)
<pm> add @nestjs/config

# TypeORM + PostgreSQL
<pm> add @nestjs/typeorm typeorm pg

# Prisma
<pm> add @prisma/client && <pm> add -D prisma

# Validation (always for NestJS)
<pm> add class-validator class-transformer

# Kafka
<pm> add @nestjs/microservices kafkajs

# RabbitMQ
<pm> add @nestjs/microservices amqplib amqp-connection-manager

# HTTP security — Fastify
<pm> add @fastify/helmet @nestjs/throttler

# HTTP security — Express
<pm> add helmet @nestjs/throttler

# Logger: nestjs-pino
<pm> add nestjs-pino pino-http && <pm> add -D pino-pretty

# Swagger (always)
<pm> add @nestjs/swagger

# Redis
# cache-manager-ioredis-yet: verify maintenance status before use — check npm for recent activity
<pm> add @nestjs/cache-manager cache-manager cache-manager-ioredis-yet
```

**Step 5 — Replace Prettier with Biome; configure typescript-eslint**

```bash
<pm> remove @nestjs/eslint-config eslint-config-prettier prettier
rm -f .eslintrc.js .eslintrc.cjs .prettierrc .prettierignore
```

Load `engineering-linting` and follow it in full. Then add `.vscode/extensions.json`:

```json
{
  "recommendations": ["biomejs.biome", "dbaeumer.vscode-eslint", "Prisma.prisma"]
}
```

Remove `"Prisma.prisma"` if Prisma was not selected.

**Step 6 — Security and bootstrap setup**

If HTTP security is "none": add only the global ValidationPipe to `main.ts`:

```typescript
app.useGlobalPipes(
  new ValidationPipe({ transform: true, whitelist: true, forbidNonWhitelisted: true }),
);
```

This option set must stay identical to the one in the `engineering-nestjs-security` bootstrap — that file is the canonical owner; this branch exists only for projects that skipped HTTP security. The pipe is mandatory regardless of security choice — without it, `class-validator` decorators are silently ignored.

If HTTP security was selected: load and follow `engineering-nestjs-security` in full (it includes `useGlobalPipes`) before proceeding.

**Step 7 — Logger setup**

Skip if Logger was set to "NestJS built-in". Otherwise load and follow `engineering-nestjs-logger` in full.

**Step 8 — Global interceptors and exception filter**

Skip if all interceptors were set to "none". Otherwise load and follow `engineering-nestjs-interceptors` in full.

**Step 9 — Install and configure Vitest**

```bash
<pm> add -D vitest @vitest/coverage-v8 unplugin-swc @swc/core
```

Remove the Jest configuration that `nest new` injects into `package.json`:

```bash
<pm> remove jest @types/jest ts-jest @nestjs/testing
```

Then manually remove the `jest` config block and any `@jest/*` entries from `package.json`. Verify with `npm list jest` — it must not appear after removal. Create `vitest.config.ts` — see `engineering-testing` for the reference config.

**Step 10 — Verify path aliases**

Path aliases were configured in Step 3. Verify now that all source files written in Steps 6–9 use aliases (`@common/`, `@domain/`, `@application/`, etc.) and not relative paths. If any file uses a relative import across layer boundaries, update it to use the alias.

```bash
# find any relative cross-layer imports still present
grep -rn "from '\.\./\.\." src/
```

Every import that crosses a layer boundary must use an alias. Relative paths are only acceptable within the same directory (e.g., `'./domain.exception.js'` inside `common/exceptions/`).

**Step 11 — Update package.json scripts**

```json
{
  "scripts": {
    "lint": "biome check --write src && eslint --fix src",
    "lint:check": "biome check src && eslint src",
    "test": "vitest run",
    "test:watch": "vitest",
    "test:cov": "vitest run --coverage"
  }
}
```

Verify: run `npm run lint:check` and `npm run test` before proceeding.

**Step 12 — Scaffold domain structure**

Only after steps 1–11 are complete:

1. Load `engineering-implement`. The STOP gate in that skill is mandatory — post the implementation checklist and wait for user confirmation before writing any domain entity, use case, or repository.
2. Then load the architecture skill:

| Architecture | Load skill |
|---|---|
| Hexagonal | `engineering-arch-hexagonal` |
| Modular monolith | `engineering-arch-modular` |
| MVC | `engineering-arch-mvc` |

**Step 13 — Docker setup**

Load `engineering-docker` and generate in order: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `docker/entrypoint-development.sh`. Never generate before project structure and `package.json` are finalized.

---

## Native packages first

Always prefer `@nestjs/*` packages over third-party alternatives.

| Concern | Use | Never use instead |
|---|---|---|
| Env validation | `@nestjs/config` + Joi | Zod, dotenv-safe, custom parsers |
| Request validation | `class-validator` + `class-transformer` + `ValidationPipe` | Zod, Yup, Joi for request bodies |
| HTTP client | `@nestjs/axios` | raw `axios`, `node-fetch`, `undici` |
| Caching | `@nestjs/cache-manager` | direct `ioredis` for caching logic — **exception:** distributed locks, idempotency keys, and rate limiting counters require atomic Redis primitives not exposed by `@nestjs/cache-manager`; for those, use `ioredis` directly as specified in `engineering-concurrency` |
| Rate limiting | `@nestjs/throttler` | custom middleware, `express-rate-limit` |
| Scheduling | `@nestjs/schedule` | `node-cron`, `bull` for simple scheduling |
| JWT | `@nestjs/jwt` + `@nestjs/passport` | raw `jsonwebtoken` |
| WebSockets | `@nestjs/websockets` + `@nestjs/platform-socket.io` | raw `socket.io` |
| Swagger | `@nestjs/swagger` | `swagger-ui-express` directly |
| Testing module | `@nestjs/testing` | manual class instantiation |

---

## Module structure

- **AppModule (root):** global concerns only — ConfigModule, global guards/interceptors/filters, feature module imports.
- **Feature modules:** flat — one `.module.ts` per feature. Each owns its controllers, use cases, services, repository bindings.
- Never re-declare providers already registered in global modules (including `PrismaService` and `HttpService`).

---

## Versioning

Use NestJS URI versioning — never prefix routes manually with `v1/` in `@Controller()`.

```typescript
// main.ts — enable versioning once at bootstrap
import { VersioningType } from '@nestjs/common';
app.enableVersioning({ type: VersioningType.URI, defaultVersion: '1' });

// controller — declare path + version separately
@Controller({ path: 'users', version: '1' })
export class UserController {}
// resolves to: /api/v1/users (the global 'api' prefix is set by the
// production bootstrap in engineering-nestjs-security; without it: /v1/users)
```

**Wrong:**
```typescript
@Controller('v1/users')   // ← hard-coded prefix — cannot be managed by NestJS versioning API
```

`enableVersioning` lets you bump versions per-controller without touching routes in other controllers. It also enables `@Version(['1', '2'])` for co-existing versions during migration.

---

## Swagger

Swagger must never be enabled in production — calling `SwaggerModule.setup()` unconditionally exposes the API schema, an information-disclosure risk. The canonical setup block (gated via `ConfigService`, never via direct `process.env` reads) lives in the production `main.ts` owned by `engineering-nestjs-security`. This skill owns only the rule and the controller-level decorators (`@ApiTags`, response types).

---

## Controllers

- Controllers are thin — no business logic.
- Single responsibility: HTTP request → call use case → return HTTP response.
- Validate via DTO + `ValidationPipe` (whitelist + forbidNonWhitelisted + transform).
- Map DTO → domain input at the controller boundary. Never pass DTOs into use cases.
- Swagger decorators on every public endpoint: `@ApiTags`, `@ApiOkResponse`, `@ApiBadRequestResponse`, `@ApiInternalServerErrorResponse`.
- Class-level: responses common to **all** endpoints — always include `@ApiBadRequestResponse`, `@ApiTooManyRequestsResponse`, and `@ApiInternalServerErrorResponse`. Method-level: success type and endpoint-specific errors (e.g., `@ApiConflictResponse` only on the register endpoint).
- POST that creates: default 201 + `@ApiCreatedResponse`. POST that queries: `@HttpCode(HttpStatus.OK)` + `@ApiOkResponse`.

**Response shape rule:** every controller method must declare an explicit response DTO as return type. Never return an inline anonymous object — the controller does not decide what fields to expose.

```typescript
// wrong — controller decides the response shape inline
async register(@Body() dto: RegisterUserRequestDto): Promise<{ id: string }> {
  const output = await this.registerUser.execute({ ... });
  return { id: output.id };   // ← controller picking fields arbitrarily
}

// correct — response DTO defines the contract; controller maps to it
async register(@Body() dto: RegisterUserRequestDto): Promise<RegisterUserResponseDto> {
  return this.registerUser.execute({ email: dto.email, password: dto.password, name: dto.name });
  // use case output already matches the response DTO shape, no manual mapping needed
}
```

If the use case output shape differs from the HTTP response shape, create a presentation mapper or map in the controller — but always return a named DTO type.

---

## Dependency injection

- `@Inject(TOKEN)` for interface-backed providers. Omit `@Inject` for concrete class providers — NestJS resolves by type.
- Never use `forwardRef` — it indicates a circular dependency that must be resolved structurally.
- Services must be stateless. No mutable instance variables that accumulate state across requests. (see `engineering-design-patterns`, Singleton section)
- Use `DEFAULT` (singleton) scope. `REQUEST` scope only when explicitly required and justified.

---

## Global concerns

Register via token providers on `AppModule`. **Never use `app.useGlobalFilters()`, `app.useGlobalGuards()`, or `app.useGlobalInterceptors()`** — those methods register outside the NestJS DI container, which means the registered class cannot receive injected dependencies.

```typescript
// wrong — registered outside DI, no dependency injection available
app.useGlobalFilters(new HttpExceptionFilter());
app.useGlobalGuards(new ThrottlerBehindProxyGuard());
app.useGlobalInterceptors(new TraceIdInterceptor());

// correct — registered inside DI container, full injection support
@Module({
  providers: [
    { provide: APP_FILTER, useClass: HttpExceptionFilter },
    { provide: APP_GUARD, useClass: ThrottlerBehindProxyGuard },
    { provide: APP_INTERCEPTOR, useClass: TraceIdInterceptor },
    { provide: APP_INTERCEPTOR, useClass: TransformResponseInterceptor },
  ],
})
export class AppModule {}
```

`ThrottlerBehindProxyGuard` must be registered via `APP_GUARD` — without this binding, `ThrottlerModule.forRoot` is configured but no route is protected.

The concrete classes in this example are owned elsewhere: `ThrottlerBehindProxyGuard` by `engineering-nestjs-security`; `HttpExceptionFilter`, `TraceIdInterceptor`, and `TransformResponseInterceptor` by `engineering-nestjs-interceptors`. This section owns only the APP_* registration rule.

Never duplicate these in feature modules.

---

## DTOs

- Every controller input (body, query, params) is a typed DTO class with `class-validator` decorators.
- DTOs never leak into domain entities or use cases — map at controller boundary.
- Every field exposed via Swagger must have `@ApiProperty()` or `@ApiPropertyOptional()`.
- User-facing text inputs must have `@MaxLength()`.
- `Date` fields must declare `{ type: String, format: 'date-time' }` in `@ApiProperty` — without it, Swagger infers the type as `object` and omits the format, making the generated spec ambiguous:

```typescript
@ApiProperty({ type: String, format: 'date-time' })
declare createdAt: Date;
```

**Naming convention for presentation-layer DTOs:**

| Role | Pattern | Example |
|---|---|---|
| HTTP request body | `<Action><Resource>RequestDto` | `RegisterUserRequestDto` |
| HTTP response | `<Action><Resource>ResponseDto` | `RegisterUserResponseDto` |
| Query params | `<Action><Resource>QueryDto` | `ListUsersQueryDto` |

Never use the unqualified `<Action><Resource>Dto` for presentation DTOs — it does not communicate direction (input vs output) and conflicts with internal use case DTOs (`RegisterUserInput`, `RegisterUserOutput`) that live in `application/usecases/`.

---

## Configuration

Validate all env vars at startup via `@nestjs/config` with a Joi schema. Missing required vars throw at boot, not at runtime.

```typescript
ConfigModule.forRoot({
  isGlobal: true,
  validationSchema: Joi.object({
    NODE_ENV: Joi.string().valid('development', 'production', 'test').required(),
    PORT: Joi.number().default(3000),
    DATABASE_URL: Joi.string().required(),
    REDIS_URL: Joi.string().required(),
  }),
  validationOptions: {
    allowUnknown: false,
    abortEarly: false,
  },
})
```

- Never read `process.env` directly outside the config validation module.
- Inject `ConfigService` and use `getOrThrow` for required values.

---

## Logging

Structured logging via injected logger — never `console.log`.

| Level | When |
|---|---|
| `debug` | Dev-only internals: token counts, cache hits, intermediate state |
| `log` | Significant business events: operation started/completed, state transitions |
| `warn` | Recoverable anomalies: retry triggered, item skipped, threshold exceeded |
| `error` | Unrecoverable failures — include full error object and context |

If nestjs-pino was selected: inject the `ILogger` contract via `LOGGER_TOKEN` — never `PinoLogger` directly. The contract, token, and injection pattern are owned by `engineering-nestjs-logger`.

If Logger is NestJS built-in: use `@nestjs/common` `Logger` and `(message, context)` signature instead. Never mix both in the same project. Never log credentials, tokens, PII, or raw user input.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
