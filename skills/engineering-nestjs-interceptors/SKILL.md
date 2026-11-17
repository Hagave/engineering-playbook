---
name: engineering-nestjs-interceptors
description: "**Opt-in for NestJS:** load only when NestJS is the chosen framework. NestJS global interceptors and exception filter: domain exception hierarchy, TraceIdInterceptor, TransformResponseInterceptor with IStandardResponse<T>, HttpExceptionFilter with domain-to-HTTP status mapping. Loaded by engineering-nestjs Step 8 when interceptors are selected."
version: 2.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# NestJS — Global Interceptors and Exception Filter

Loaded from `engineering-nestjs` Step 8. Create only the files for the interceptors selected in Step 1. Apply the AppModule registration at the end.

---

## File placement

| Architecture | Path |
|---|---|
| Hexagonal | `src/common/interceptors/`, `src/common/filters/`, `src/common/exceptions/` |
| MVC / Modular monolith | `src/common/interceptors/`, `src/common/filters/`, `src/common/exceptions/` |

Guards, filters, and interceptors are cross-cutting concerns — they belong in `common/` regardless of architecture. `common/` is importable by any layer and has no layer dependency. `infrastructure/` is the wrong location: it implies coupling to a specific infrastructure adapter.

**HTTP adapter:** The interceptors and filter below use Fastify types (`FastifyRequest`, `FastifyReply`). This is consistent with `engineering-nestjs-security` which bootstraps the app with `FastifyAdapter`. If the project uses Express (no HTTP security selected), replace `FastifyRequest`/`FastifyReply` with `Request`/`Response` from `'express'` and update `reply.status(n).send()` to `reply.status(n).json()`.

---

## Step 8.1 — Domain exception base classes

Create these before the filter. The filter maps domain exceptions to HTTP status codes.

```typescript
// exceptions/domain.exception.ts
export abstract class DomainException extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = this.constructor.name;
  }
}
```

**`code` is the client's discriminator — the HTTP status is only the category.** A `403` can be "not your resource", "email not verified", "account suspended" — the front-end cannot branch on the status alone. Every domain exception therefore carries a specific, stable `code` on the instance, and the filter forwards `exception.code` verbatim. Two rules:

- **Distinct reason → distinct code.** When more than one business reason maps to the same HTTP status, each must carry its own `code`. Never let two different failures collapse to the same generic code (`FORBIDDEN`) just because they share a status.
- Each semantic base class below sets a **category default** (`NOT_FOUND`, `CONFLICT`, …) so trivial cases stay terse, but you **override** it whenever the front must tell reasons apart:

```typescript
// generic — fine when there is only one 403 in this context
throw new DomainForbiddenException('Access denied');
// specific — the front must distinguish this from other 403s
throw new DomainForbiddenException('Email address is not verified', 'EMAIL_NOT_VERIFIED');
```

For a reason thrown from more than one place, promote it to a named subclass so the code and message live in one spot and cannot drift:

```typescript
// exceptions/email-not-verified.exception.ts
import { DomainForbiddenException } from './forbidden.exception';
export class EmailNotVerifiedException extends DomainForbiddenException {
  constructor() {
    super('Email address is not verified', 'EMAIL_NOT_VERIFIED');
  }
}
```

Produces exactly:

```json
{ "statusCode": 403, "error": { "code": "EMAIL_NOT_VERIFIED", "message": "Email address is not verified" } }
```

Create only the exceptions that have an immediate consumer in the current implementation. The list below is a complete reference — do not create all of them upfront. An exception class with no consumer is dead code and will fail audit.

**Exceptions with immediate consumers (always create these):**
- `DomainNotFoundException` — Prisma P2025 in update/delete operations; entity-not-found guards
- `ConflictException` — Prisma P2002, used by `mapPrismaError` in every `save()` call

**Exceptions to create only when the feature that throws them is implemented:**
- `BusinessRuleException` — create when a domain invariant violation needs to map to 422
- `ValidationException` — create when a use case must reject input that passed `ValidationPipe`; note: `ValidationPipe` itself throws NestJS's own `HttpException` (400), not this class

**Exceptions to create only when the feature that throws them is implemented:**
- `DomainUnauthorizedException` — create when JWT auth guard is implemented
- `DomainForbiddenException` — create when role/permission guards are implemented
- `ExternalServiceException` — create when the first external HTTP client is added
- `DuplicateRequestException` — create when idempotency key middleware is added

```typescript
// exceptions/not-found.exception.ts
import { DomainException } from './domain.exception';
export class DomainNotFoundException extends DomainException {
  constructor(message: string, code = 'NOT_FOUND') {
    super(message, code);
  }
}
```

Create `business-rule.exception.ts` and `validation.exception.ts` only when a use case or domain entity actually throws them.

`ConflictException` is used by `engineering-prisma`'s `mapPrismaError` for Prisma P2002 (unique constraint) errors. `DuplicateRequestException` is for idempotency key conflicts — it carries a `conflictId` field. Both map to HTTP 409 but serve different semantics. Create `ConflictException` now; create `DuplicateRequestException` only when idempotency is implemented:

```typescript
// exceptions/conflict.exception.ts
import { DomainException } from './domain.exception';
export class ConflictException extends DomainException {
  constructor(message: string, code = 'CONFLICT') {
    super(message, code);
  }
}
```

`DuplicateRequestException` has an extra field — create it separately:

```typescript
// exceptions/duplicate-request.exception.ts
import { DomainException } from './domain.exception';
export class DuplicateRequestException extends DomainException {
  constructor(message: string, public readonly conflictId: string) {
    super(message, 'DUPLICATE_REQUEST');
  }
}
```

> **`conflictId` semantics:** pass the existing resource ID when a duplicate resource was detected, or the idempotency key when a concurrent duplicate request is in flight. The client receives this value in `X-Resource-Id` to avoid an extra lookup.

> **Naming note:** Use the `Domain` prefix or a name absent from `@nestjs/common` (`DomainNotFoundException`, `DomainUnauthorizedException`, `DomainForbiddenException`). Never import NestJS exception classes and domain exception classes in the same file — `instanceof` checks in the filter will silently fail for whichever is shadowed.

> **Exception message rule:** Never include internal identifiers (database IDs, UUIDs, email addresses) in exception messages. For 4xx, `HttpExceptionFilter` forwards `exception.message` to the client — including an ID enables enumeration attacks and leaks persistence internals. Use only the resource name: `throw new DomainNotFoundException('User')`, not `throw new DomainNotFoundException(\`User not found: ${id}\`)`. (5xx never forward the message at all — the client always gets the fixed `INTERNAL_ERROR` envelope — but keep domain messages clean regardless, since the same exception type may map to a 4xx.)

---

## Step 8.2 — Interceptors and filter

**`trace-id.interceptor.ts`** — must be registered before all other interceptors so the trace ID is on the request when they run:

```typescript
import { CallHandler, ExecutionContext, Injectable, NestInterceptor } from '@nestjs/common';
import { randomUUID } from 'node:crypto';
import { Observable } from 'rxjs';
import { FastifyReply, FastifyRequest } from 'fastify';

export type RequestWithTrace = FastifyRequest & { traceId: string };

@Injectable()
export class TraceIdInterceptor implements NestInterceptor {
  intercept(context: ExecutionContext, next: CallHandler): Observable<unknown> {
    const ctx = context.switchToHttp();
    const request = ctx.getRequest<RequestWithTrace>();
    const response = ctx.getResponse<FastifyReply>();

    const traceId = (request.headers['x-trace-id'] as string | undefined) ?? randomUUID();
    request.traceId = traceId;
    response.header('x-trace-id', traceId);

    return next.handle();
  }
}
```

**`transform-response.interceptor.ts`** — wraps all successful responses in a standard envelope. Update all Swagger `@ApiOkResponse`/`@ApiCreatedResponse` types to reflect `IStandardResponse<T>`:

> **Envelope contract — client gets only what it needs.** The body carries `statusCode` + `data` and nothing else. `path` (the client knows the URL it called), `timestamp` (the client has a clock), and `traceId` are **never** in the body. Correlation travels in the `x-trace-id` response header, which `TraceIdInterceptor` already sets on every response — success and error alike. This keeps the body minimal and stops debug/correlation metadata from becoming part of the public contract. The error envelope (in the filter below) mirrors this shape: `statusCode` + `error`, no metadata.

```typescript
import { CallHandler, ExecutionContext, Injectable, NestInterceptor } from '@nestjs/common';
import { Observable, map } from 'rxjs';
import { FastifyReply } from 'fastify';

export interface IStandardResponse<T> {
  statusCode: number;
  data: T;
}

@Injectable()
export class TransformResponseInterceptor<T>
  implements NestInterceptor<T, IStandardResponse<T>>
{
  intercept(context: ExecutionContext, next: CallHandler<T>): Observable<IStandardResponse<T>> {
    const response = context.switchToHttp().getResponse<FastifyReply>();

    return next.handle().pipe(
      map((data): IStandardResponse<T> => ({
        statusCode: response.statusCode,
        data,
      })),
    );
  }
}
```

**`http-exception.filter.ts`** — maps domain exceptions to HTTP status codes. It is the single place that decides what the client sees. Two invariants:

1. **Full detail to the logs, sanitized envelope to the client.** Every exception is logged with full context (stack, method, url, traceId): 5xx at `error`, 4xx at `warn` — nothing reaches the boundary unlogged. A 5xx **never** puts the original `exception.message` or the exception class name in the body — those are internal detail (raw ORM/infra text, class names) and go only to the logs; the client gets a fixed `INTERNAL_ERROR` / "An unexpected error occurred". 4xx are domain/`HttpException` errors whose messages are already client-safe (they must never carry IDs — see the exception message rule below).
2. **Specific, stable `code` from the exception instance.** The client discriminates on `error.code` — a stable string that names the actual reason (`EMAIL_NOT_VERIFIED`), not the HTTP category (`FORBIDDEN`) and not the class name. It is read from `exception.code` (see Step 8.1), so two failures sharing a status still carry distinct codes, and renaming a class never breaks a consumer. Framework `HttpException`s that have no `code` fall back to a per-status category code.

Import only the exception classes that were actually created in Step 8.1. Do not import exceptions that do not yet have files — the build will fail. Add imports and `STATUS_MAP` entries incrementally as each optional exception is created.

```typescript
import { ArgumentsHost, Catch, ExceptionFilter, HttpException, HttpStatus } from '@nestjs/common';
import { InjectPinoLogger, PinoLogger } from 'nestjs-pino';
import { FastifyReply } from 'fastify';
import { ConflictException } from '@common/exceptions/conflict.exception';
import { DomainException } from '@common/exceptions/domain.exception';
import { DomainNotFoundException } from '@common/exceptions/not-found.exception';
import type { RequestWithTrace } from '@common/interceptors/trace-id.interceptor';
// Add these imports only when their exception files are created:
// import { ValidationException } from '@common/exceptions/validation.exception';
// import { BusinessRuleException } from '@common/exceptions/business-rule.exception';
// import { DuplicateRequestException } from '@common/exceptions/duplicate-request.exception';
// import { DomainUnauthorizedException } from '@common/exceptions/unauthorized.exception';
// import { DomainForbiddenException } from '@common/exceptions/forbidden.exception';
// import { ExternalServiceException } from '@common/exceptions/external-service.exception';

interface IErrorResponse {
  statusCode: number;
  error: { code: string; message: string };
}

@Catch()
export class HttpExceptionFilter implements ExceptionFilter {
  constructor(
    @InjectPinoLogger(HttpExceptionFilter.name)
    private readonly logger: PinoLogger,
  ) {}

  catch(exception: unknown, host: ArgumentsHost): void {
    const ctx = host.switchToHttp();
    const reply = ctx.getResponse<FastifyReply>();
    const request = ctx.getRequest<RequestWithTrace>();

    const traceId = request.traceId ?? '';
    const status = this.resolveHttpStatus(exception);
    const isServerError = status >= 500;

    // Full context to the server logs — never to the client. Nothing reaches the
    // boundary unlogged: 5xx (unexpected) at error with the stack, 4xx at warn.
    const logContext = { err: exception, traceId, method: request.method, url: request.url };
    if (isServerError) this.logger.error(logContext, 'Unhandled server error');
    else this.logger.warn(logContext, 'Handled client error');

    // Uncomment when DuplicateRequestException is created:
    // if (exception instanceof DuplicateRequestException) {
    //   reply.header('X-Resource-Id', exception.conflictId);
    // }

    // 5xx is sanitized to a fixed code/message; 4xx sources are client-safe.
    const body: IErrorResponse = isServerError
      ? { statusCode: status, error: { code: 'INTERNAL_ERROR', message: 'An unexpected error occurred' } }
      : {
          statusCode: status,
          error: { code: this.resolveErrorCode(exception, status), message: this.resolveClientMessage(exception) },
        };

    // traceId is echoed via the header (not the body) so the client can quote it
    // to support without the correlation ID becoming part of the response contract.
    reply.header('x-trace-id', traceId).status(status).send(body);
  }

  // DomainException must be last — it is the superclass and would match before subclasses.
  // Each entry maps [Type, HTTP status] only. The client `code` comes from the exception
  // instance (`exception.code`), never from this table — that is what lets two different
  // 403s carry distinct codes. Add entries only when their files exist (see Step 8.1).
  private static readonly STATUS_MAP = [
    [DomainNotFoundException,     HttpStatus.NOT_FOUND],
    [ConflictException,           HttpStatus.CONFLICT],
    [DomainException,             HttpStatus.UNPROCESSABLE_ENTITY],
    // Add when created:
    // [ValidationException,         HttpStatus.BAD_REQUEST],
    // [BusinessRuleException,        HttpStatus.UNPROCESSABLE_ENTITY],
    // [DomainUnauthorizedException,  HttpStatus.UNAUTHORIZED],
    // [DomainForbiddenException,     HttpStatus.FORBIDDEN],
    // [DuplicateRequestException,    HttpStatus.CONFLICT],
    // [ExternalServiceException,     HttpStatus.BAD_GATEWAY],
  ] as const;

  // Fallback codes for framework HttpExceptions (e.g. ValidationPipe 400) that are not
  // DomainExceptions and therefore carry no `code`.
  private static readonly HTTP_STATUS_CODE: Record<number, string> = {
    [HttpStatus.BAD_REQUEST]:           'VALIDATION_ERROR',
    [HttpStatus.UNAUTHORIZED]:          'UNAUTHORIZED',
    [HttpStatus.FORBIDDEN]:             'FORBIDDEN',
    [HttpStatus.NOT_FOUND]:             'NOT_FOUND',
    [HttpStatus.CONFLICT]:              'CONFLICT',
    [HttpStatus.UNPROCESSABLE_ENTITY]:  'BUSINESS_RULE_VIOLATION',
  };

  private resolveHttpStatus(exception: unknown): number {
    if (exception instanceof HttpException) return exception.getStatus();
    const match = HttpExceptionFilter.STATUS_MAP.find(([Type]) => exception instanceof Type);
    return match?.[1] ?? HttpStatus.INTERNAL_SERVER_ERROR;
  }

  private resolveErrorCode(exception: unknown, status: number): string {
    // Domain exceptions carry their own specific code (e.g. 'EMAIL_NOT_VERIFIED').
    if (exception instanceof DomainException) return exception.code;
    // Framework HttpExceptions (ValidationPipe, guards) fall back to a category code.
    return HttpExceptionFilter.HTTP_STATUS_CODE[status] ?? 'ERROR';
  }

  // Only called for 4xx — every source here (domain exception, HttpException) is
  // already client-safe. 5xx never reaches this method.
  private resolveClientMessage(exception: unknown): string {
    if (exception instanceof HttpException) {
      const response = exception.getResponse();
      if (typeof response === 'string') return response;
      if (typeof response === 'object' && response !== null && 'message' in response) {
        const msg = (response as { message: string | string[] }).message;
        return Array.isArray(msg) ? msg.join(', ') : msg;
      }
    }
    if (exception instanceof DomainException) return exception.message;
    return 'An error occurred';
  }
}
```

---

## AppModule registration

`TraceIdInterceptor` must be first — the trace ID must be on the request before any other interceptor or the filter reads it. Include only the providers for the interceptors selected in Step 1.

All architectures use `common/` paths:

```typescript
import { APP_FILTER, APP_INTERCEPTOR } from '@nestjs/core';
import { HttpExceptionFilter } from '@common/filters/http-exception.filter';
import { TraceIdInterceptor } from '@common/interceptors/trace-id.interceptor';
import { TransformResponseInterceptor } from '@common/interceptors/transform-response.interceptor';

// providers:
{ provide: APP_FILTER, useClass: HttpExceptionFilter },
{ provide: APP_INTERCEPTOR, useClass: TraceIdInterceptor },
{ provide: APP_INTERCEPTOR, useClass: TransformResponseInterceptor },
```

---

## Completion gate

Before returning to `engineering-nestjs` Step 9, verify:
- `AppModule` providers array contains one `APP_FILTER` entry and one `APP_INTERCEPTOR` per selected interceptor.
- `TraceIdInterceptor` is registered **before** `TransformResponseInterceptor` — order matters.
- All exception class files exist at the correct path for the active architecture.
- No import in any file mixes `@nestjs/common` exception types with domain exception types.
- No response body (success or error) contains `path`, `timestamp`, or `traceId` — correlation is the `x-trace-id` header only.
- A 5xx error body never contains the original `exception.message` or the exception class name; only `INTERNAL_ERROR` / "An unexpected error occurred".
- Every exception is logged at the boundary (5xx `error`, 4xx `warn`) with `err`, `traceId`, `method`, `url`.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
