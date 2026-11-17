---
name: engineering-nestjs-security
description: "**Opt-in for NestJS:** load only when NestJS is the chosen framework. NestJS HTTP security setup: helmet (CSP), CORS with multi-origin callback, ThrottlerBehindProxyGuard, CSRF protection (cookie/session auth), and production-ready main.ts bootstrap. Loaded by engineering-nestjs Step 6 when HTTP security is selected."
version: 1.1.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# NestJS — HTTP Security

Loaded from `engineering-nestjs` Step 6. Apply this entire step before moving to Step 7.

---

## Packages

**Fastify:** `@fastify/helmet @nestjs/throttler` — installed via `engineering-nestjs` Step 4 (Fastify branch).
**Express:** `helmet @nestjs/throttler` — replace `app.register(helmet, ...)` with `app.use(helmet({ contentSecurityPolicy: { directives: { ... } } }))` throughout.
**CSRF (only for cookie/session auth):** Layers 1–2 (cookie hardening + `CsrfGuard`) need **no extra packages** — just `@nestjs/config` and `Reflector`, already present. Layer 3 (optional token) adds Fastify `@fastify/cookie @fastify/csrf-protection` or Express `csrf-csrf cookie-parser`. Skip all of it for stateless Bearer-token APIs — see the CSRF protection section.

---

## main.ts — production bootstrap

Replace the placeholder `main.ts` from Step 2 with this reference. Adjust project name, description, and version in the Swagger block.

```typescript
import { NestFactory } from '@nestjs/core';
import { ValidationPipe, VersioningType } from '@nestjs/common';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';
import { SwaggerModule, DocumentBuilder } from '@nestjs/swagger';
// Logger: nestjs-pino only — remove this import if Logger was set to "NestJS built-in"
import { Logger } from 'nestjs-pino';
import { ConfigService } from '@nestjs/config';
import helmet from '@fastify/helmet';
// Adapt path to active architecture:
// Hexagonal:          './presentation/modules/app.module'
// MVC/Modular:        './app.module'
import { AppModule } from './presentation/modules/app.module';

async function bootstrap(): Promise<void> {
  const app = await NestFactory.create<NestFastifyApplication>(
    AppModule,
    new FastifyAdapter({ trustProxy: true }),
    { bufferLogs: true },
  );

  // Logger: nestjs-pino only — remove this line if Logger was set to "NestJS built-in"
  app.useLogger(app.get(Logger));

  const config = app.get(ConfigService);
  const isDev = config.get<string>('NODE_ENV') === 'development';
  const corsOrigins = config
    .get<string>('CORS_ORIGIN', '')
    .split(',')
    .map((o): string => o.trim())
    .filter(Boolean);

  await app.register(helmet, {
    hsts: isDev ? false : { maxAge: 31_536_000, includeSubDomains: true },
    contentSecurityPolicy: {
      directives: {
        defaultSrc: [`'self'`],
        styleSrc: [`'self'`, `'unsafe-inline'`],
        imgSrc: [`'self'`, 'data:', 'validator.swagger.io'],
        scriptSrc: [`'self'`, `'unsafe-inline'`],
      },
    },
  });

  app.enableCors({
    origin: (requestOrigin, callback): void => {
      if (
        !requestOrigin ||
        corsOrigins.includes(requestOrigin) ||
        (isDev && /^https?:\/\/localhost(:\d+)?$/.test(requestOrigin))
      ) {
        return callback(null, true);
      }
      callback(null, false);
    },
    methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'x-trace-id', 'Idempotency-Key'],
    exposedHeaders: ['x-trace-id'],
    credentials: false,
    maxAge: 86_400,
  });

  app.setGlobalPrefix('api');

  app.enableVersioning({ type: VersioningType.URI, defaultVersion: '1' });

  app.useGlobalPipes(
    new ValidationPipe({ transform: true, whitelist: true, forbidNonWhitelisted: true }),
  );

  if (isDev) {
    const swaggerConfig = new DocumentBuilder()
      .setTitle('<project name>')
      .setDescription('<description>')
      .setVersion('1.0')
      .addBearerAuth()
      .build();
    const document = SwaggerModule.createDocument(app, swaggerConfig);
    SwaggerModule.setup('docs', app, document, { useGlobalPrefix: true });
  }

  const port = config.get<number>('PORT', 3000);
  await app.listen(port, '0.0.0.0');
}

void bootstrap();
```

Notes:
- `trustProxy: true` — required behind any reverse proxy or load balancer; without it, `ThrottlerBehindProxyGuard` reads the proxy IP instead of the real client IP.
- `bufferLogs: true` — Nest holds log messages until Pino is initialized, preventing boot-time log loss.
- If Logger was set to "NestJS built-in": remove both `import { Logger } from 'nestjs-pino'` and `app.useLogger(app.get(Logger))`. Removing only the call and leaving the import creates a dead import that violates the no-dead-code rule and will crash if `LoggerModule` is not registered.
- Swagger is always dev-only. Never expose API docs in production.
- `CORS_ORIGIN` accepts comma-separated origins. Localhost is auto-allowed in development.
- `exposedHeaders: ['x-trace-id']` — without this, browsers block the frontend from reading the header even when the backend sends it. Use lowercase — HTTP/2 mandates lowercase header names and fetch normalizes to lowercase on read.

---

## ThrottlerBehindProxyGuard

`ThrottlerGuard` (the default) reads `req.ip`, which behind a reverse proxy without `trustProxy` is always the proxy's IP — all clients share the same rate-limit bucket. `ThrottlerBehindProxyGuard` delegates to `req.ip` after Fastify's proxy resolution.

Create in `src/common/guards/` — guards are cross-cutting concerns and belong in `common/` regardless of architecture:

```typescript
// throttler-behind-proxy.guard.ts
import { Injectable } from '@nestjs/common';
import { ThrottlerGuard } from '@nestjs/throttler';
import type { FastifyRequest } from 'fastify';

@Injectable()
export class ThrottlerBehindProxyGuard extends ThrottlerGuard {
  protected getTracker(req: Record<string, unknown>): Promise<string> {
    // req.ip is resolved by Fastify based on trustProxy (set in main.ts):
    //   - direct connections: socket.remoteAddress (real IP, cannot be spoofed)
    //   - proxied connections: first IP from x-forwarded-for (real client IP when the
    //     reverse proxy controls the header — configure nginx/LB to strip incoming XFF)
    // Never parse x-forwarded-for directly: position [0] is client-writable regardless
    // of array/string handling, allowing IP rotation to bypass per-IP rate limiting.
    return Promise.resolve((req as unknown as FastifyRequest).ip ?? 'unknown');
  }
}
```

---

## AppModule additions

```typescript
import { ThrottlerModule } from '@nestjs/throttler';
import { APP_GUARD } from '@nestjs/core';
import { ThrottlerBehindProxyGuard } from '@common/guards/throttler-behind-proxy.guard';

// imports:
// @nestjs/throttler v5+: ttl is in milliseconds — 60_000 = 60 seconds, 30 req/min
ThrottlerModule.forRoot([{ ttl: 60_000, limit: 30 }]),

// providers:
{ provide: APP_GUARD, useClass: ThrottlerBehindProxyGuard },
```

`ThrottlerModule.forRoot` uses static config — throttle limits are not runtime-dynamic, so `forRootAsync` adds complexity with no benefit.

---

## CSRF protection

**Applicability gate — read before adding anything.** CSRF is an attack against
*ambient credentials*: cookies or sessions the browser attaches automatically to
cross-site requests. It does **not** apply to a stateless API authenticated by a
`Authorization: Bearer <token>` header, because the browser never auto-sends that
header cross-origin. The default bootstrap above is stateless (`credentials: false`,
auth via the `Authorization` header) — **for it, CSRF protection is unnecessary and
must be skipped.** Add the setup below **only** when the app authenticates via
cookies or server-side sessions. If in doubt, stop and confirm the auth model with
the user before implementing.

When cookie/session auth is used, `enableCors` must set `credentials: true` and list
explicit origins (never `*`) — the origin allowlist is also what Layer 2 below relies on.

### Strategy — defense in depth, not one middleware

The NestJS doc ([reference](https://docs.nestjs.com/security/csrf)) shows a single
double-submit middleware. A single mechanism is a single point of failure (a
misconfigured token retriever silently disables it). Layer three independent controls
so no one misconfiguration removes protection:

| Layer | Control | Enforced by | Covers |
|-------|---------|-------------|--------|
| 1 | `SameSite=strict` + `__Host-` cookies | Browser | Blocks the cookie from riding cross-site requests at all |
| 2 | Origin/Referer allowlist guard | App (`CsrfGuard`) | Rejects any state-changing request not originating from an allowed origin |
| 3 | Double-submit token (optional) | `@fastify/csrf-protection` | Null-origin clients and defense in depth |

Layers 1–2 are the baseline. Layer 3 is added only when you must accept requests with
no trustworthy `Origin` header (e.g. certain native/mobile clients).

### Layer 1 — cookie hardening

Every auth/session cookie the app sets must carry `sameSite: 'strict'` (or `'lax'` if
top-level GET navigation must stay authenticated), `secure: true` outside dev,
`httpOnly: true`, and the `__Host-` name prefix (which forces `secure`, `path=/`, and
no `Domain` — closing subdomain injection). `SameSite=strict` alone stops the majority
of CSRF; the next layers cover the gaps (older browsers, `lax` GET holes).

### Layer 2 — Origin validation guard (primary app control)

This is the OWASP "verifying origin with standard headers" technique, implemented as a
DI-native guard so it is testable, composes with the auth guards, and supports per-route
opt-out for signature-authenticated webhooks. Guards are cross-cutting → `src/common/`.

```typescript
// common/decorators/skip-csrf.decorator.ts
import { SetMetadata, type CustomDecorator } from '@nestjs/common';

export const SKIP_CSRF = 'skipCsrf';

/** Opt a route out of CSRF origin validation — e.g. webhooks authenticated by a
 *  provider signature (Stripe, GitHub), which carry no browser origin. */
export const SkipCsrf = (): CustomDecorator => SetMetadata(SKIP_CSRF, true);
```

```typescript
// common/guards/csrf.guard.ts
import {
  CanActivate,
  ExecutionContext,
  ForbiddenException,
  Injectable,
} from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { ConfigService } from '@nestjs/config';
import type { FastifyRequest } from 'fastify';
import { SKIP_CSRF } from '@common/decorators/skip-csrf.decorator';

// Safe methods (RFC 7231) do not mutate state and are never CSRF vectors.
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

@Injectable()
export class CsrfGuard implements CanActivate {
  private readonly allowedOrigins: readonly string[];

  constructor(
    private readonly reflector: Reflector,
    config: ConfigService,
  ) {
    // Same source as CORS — a request the CORS layer would reject must also fail here.
    this.allowedOrigins = config
      .get<string>('CORS_ORIGIN', '')
      .split(',')
      .map((o): string => o.trim())
      .filter(Boolean);
  }

  canActivate(context: ExecutionContext): boolean {
    const request = context.switchToHttp().getRequest<FastifyRequest>();

    if (SAFE_METHODS.has(request.method)) {
      return true;
    }

    const skip = this.reflector.getAllAndOverride<boolean>(SKIP_CSRF, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (skip === true) {
      return true;
    }

    const origin = this.resolveOrigin(request);
    if (origin !== null && this.allowedOrigins.includes(origin)) {
      return true;
    }

    // No trustworthy origin, or origin not allowlisted → treat as forged.
    throw new ForbiddenException('CSRF validation failed: request origin not allowed.');
  }

  private resolveOrigin(request: FastifyRequest): string | null {
    const { origin, referer } = request.headers;
    if (typeof origin === 'string' && origin.length > 0) {
      return origin;
    }
    // Some agents omit Origin on same-origin writes — fall back to the Referer origin.
    if (typeof referer === 'string' && referer.length > 0) {
      try {
        return new URL(referer).origin;
      } catch {
        return null;
      }
    }
    return null;
  }
}
```

Register globally in `app.module.ts`, alongside `ThrottlerBehindProxyGuard`:

```typescript
import { CsrfGuard } from '@common/guards/csrf.guard';

// providers (order-independent; both run per request):
{ provide: APP_GUARD, useClass: CsrfGuard },
```

Design notes:
- **Fail-closed on a missing origin.** A state-changing request with no `Origin`/`Referer`
  is rejected, not allowed — the opposite bias would let the weakest clients through.
  Endpoints that legitimately have no browser origin use `@SkipCsrf()` and authenticate by
  another means (signature, mTLS).
- **No shared bucket with CORS.** CORS answers the browser's preflight; it does not block
  a same-site-forged or simple (non-preflighted) POST. This guard runs on the actual
  request, in the app, for every method — the two are complementary, not redundant.
- **Testable in isolation** — a unit test drives `canActivate` with a fake `ExecutionContext`;
  no HTTP stack, no plugin internals.

### Layer 3 — double-submit token (optional)

Add only when null-origin clients must be accepted. Install
`pnpm add @fastify/cookie @fastify/csrf-protection`; the CSRF plugin needs `@fastify/cookie`
registered **before** it. In `main.ts`, after the `helmet` registration:

```typescript
import fastifyCookie from '@fastify/cookie';
import fastifyCsrf from '@fastify/csrf-protection';

await app.register(fastifyCookie, { secret: config.get<string>('CSRF_SECRET') });
await app.register(fastifyCsrf, {
  cookieOpts: { signed: true, sameSite: 'strict', path: '/' }, // __Host- semantics via secure+path
});
```

Expose a token endpoint; `reply.generateCsrf()` sets the secret cookie and returns the
token the client echoes back in `x-csrf-token` (the plugin's default `getToken`). Add
`x-csrf-token` to the CORS `allowedHeaders`. Validate inside the guard so `@SkipCsrf()`
and the safe-method skip still apply — the plugin's global `onRequest` hook cannot see
route metadata, so do **not** wire it as a bare global hook:

```typescript
// inside CsrfGuard.canActivate (make it async), after the origin check passes,
// when Layer 3 is enabled. csrfProtection is a callback-style Fastify hook — wrap it:
const reply = context.switchToHttp().getResponse<FastifyReply>();
await new Promise<void>((resolve, reject): void => {
  reply.server.csrfProtection(request, reply, (err?: unknown): void =>
    err ? reject(err) : resolve(),
  );
});
return true; // token valid; invalid/absent token already threw
```

Wrapping the callback hook in a `Promise` keeps `canActivate` `async` without assuming a
promise-returning overload. Confirm `reply.server` (or `reply.request.server`) exposes
`csrfProtection` on the installed Fastify version.

**Express:** `csurf` is deprecated. Use `csrf-csrf` (`doubleCsrf`) with `cookie-parser`;
option names vary by major version, so verify `getSecret` and the token-retrieval callback
against the installed README. The Layer 1 and Layer 2 controls above are adapter-agnostic
and carry over unchanged.

---

## Joi env vars

Add to the validation schema in `app.module.ts`:

```typescript
CORS_ORIGIN: Joi.string().default(''),
```

When CSRF Layer 3 (the double-submit token) is enabled, also add — the secret signs the
CSRF cookie and must be stable across replicas, so it is required, not defaulted (Layers
1–2 need no secret):

```typescript
CSRF_SECRET: Joi.string().min(32).required(),
```

---

## Completion gate

Before returning to `engineering-nestjs` Step 7: start the application and confirm it boots cleanly with all required env vars present. Add `CORS_ORIGIN` to `.env.example` with a placeholder and a comment. Verify that a request to a non-allowed origin returns `403` and a request to the correct origin succeeds.

If CSRF protection was enabled: verify Layer 2 first — a `POST`/`PUT`/`PATCH`/`DELETE` with a disallowed or absent `Origin` returns `403`, the same request from an allowlisted origin succeeds, a `GET` is never blocked, and a `@SkipCsrf()` route passes regardless of origin. If Layer 3 was added, also add `CSRF_SECRET` to `.env.example` and confirm a mutation without a valid `x-csrf-token` is rejected while one carrying a freshly issued token succeeds.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
