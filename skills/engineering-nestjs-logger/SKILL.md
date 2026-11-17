---
name: engineering-nestjs-logger
description: "**Opt-in for NestJS:** load only when NestJS is the chosen framework. NestJS structured logger setup: nestjs-pino with full production config — customLogLevel, autoLogging.ignore, two-layer req/res redaction, customProps with route template, pino-pretty in dev. Loaded by engineering-nestjs Step 7 when nestjs-pino is selected."
version: 1.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# NestJS — Logger (nestjs-pino)

Loaded from `engineering-nestjs` Step 7. Apply this entire step before moving to Step 8.

---

## ILogger contract

Define the interface and token in `common/logger/logger.interface.ts` before wiring pino. Use cases and domain services inject this contract — never `PinoLogger` directly. This decouples business logic from the logging library.

```typescript
// common/logger/logger.interface.ts
export const LOGGER_TOKEN = Symbol('LOGGER');

export interface ILogger {
  log(message: string, context?: Record<string, unknown>): void;
  warn(message: string, context?: Record<string, unknown>): void;
  error(message: string, err: Error, context?: Record<string, unknown>): void;
  debug(message: string, context?: Record<string, unknown>): void;
}
```

`error()` receives the `Error` object as a dedicated second argument. Pino's built-in serializer activates only for the `err` key holding an actual `Error` instance — it produces structured JSON with `type`, `message`, and full `stack` automatically. Passing the error as a plain string (`exception.message`) or under an arbitrary key bypasses this serializer and loses stack trace structure in production logs.

Implement it in `common/logger/pino-logger.service.ts`:

```typescript
// common/logger/pino-logger.service.ts
import { Injectable } from '@nestjs/common';
import { PinoLogger, InjectPinoLogger } from 'nestjs-pino';
import type { ILogger } from './logger.interface';

@Injectable()
export class PinoLoggerService implements ILogger {
  constructor(
    @InjectPinoLogger(PinoLoggerService.name)
    private readonly logger: PinoLogger,
  ) {}

  log(message: string, context?: Record<string, unknown>): void {
    this.logger.info(context ?? {}, message);
  }

  warn(message: string, context?: Record<string, unknown>): void {
    this.logger.warn(context ?? {}, message);
  }

  error(message: string, err: Error, context?: Record<string, unknown>): void {
    this.logger.error({ err, ...context }, message);
  }

  debug(message: string, context?: Record<string, unknown>): void {
    this.logger.debug(context ?? {}, message);
  }
}
```

Register in `shared.module.ts`:

```typescript
{ provide: LOGGER_TOKEN, useClass: PinoLoggerService }
```

Inject in any service:

```typescript
import type { ILogger } from '@common/logger/logger.interface';
import { LOGGER_TOKEN } from '@common/logger/logger.interface';

constructor(
  @Inject(LOGGER_TOKEN) private readonly logger: ILogger,
) {}
```

---

If Step 6 (HTTP security) was executed, `main.ts` already includes `bufferLogs: true` and `app.useLogger(app.get(Logger))`. If Step 6 was **skipped** (HTTP security: none), update `main.ts` manually:

```typescript
const app = await NestFactory.create<NestFastifyApplication>(
  AppModule,
  new FastifyAdapter(),
  { bufferLogs: true },      // add this
);
app.useLogger(app.get(Logger));  // add this, after app is created
```

This step covers only the `AppModule` configuration.

---

## AppModule — LoggerModule

```typescript
import { LoggerModule } from 'nestjs-pino';
import { ConfigModule, ConfigService } from '@nestjs/config';
import { RequestMethod } from '@nestjs/common';
import type { IncomingMessage, ServerResponse } from 'node:http';

// pino-http callbacks receive node:http types, not FastifyRequest.
// Use a local intersection type to safely access Fastify-specific fields.
type FastifyLike = IncomingMessage & { traceId?: string; routeOptions?: { url?: string } };

// imports:
LoggerModule.forRootAsync({
  imports: [ConfigModule],
  inject: [ConfigService],
  useFactory: (config: ConfigService) => ({
    forRoutes: [{ path: '*path', method: RequestMethod.ALL }],
    pinoHttp: {
      level: config.get<string>('LOG_LEVEL', 'info'),
      transport:
        config.get<string>('NODE_ENV') !== 'production'
          ? { target: 'pino-pretty', options: { singleLine: true } }
          : undefined,
      customAttributeKeys: { responseTime: 'duration' },
      customLogLevel: (
        _req: IncomingMessage,
        res: ServerResponse,
        err: Error | undefined,
      ): 'error' | 'warn' | 'info' => {
        if (res.statusCode >= 500 || err) return 'error';
        if (res.statusCode >= 400) return 'warn';
        return 'info';
      },
      autoLogging: {
        ignore: (req: IncomingMessage): boolean => req.url === '/health', // adjust if your health endpoint path differs
      },
      serializers: {
        req: (): Record<string, never> => ({}),
        res: (): Record<string, never> => ({}),
      },
      customProps: (req: IncomingMessage): Record<string, unknown> => {
        const r = req as FastifyLike;
        return {
          traceId: r.traceId,
          route: r.routeOptions?.url ?? req.url,
        };
      },
      redact: {
        paths: ['req', 'res'],
        remove: true,
      },
    },
  }),
}),
```

Notes:
- `forRoutes: [{ path: '*path', method: RequestMethod.ALL }]` — makes pino-http handle all request logging. A separate `LoggingInterceptor` would be redundant and inferior (pino-http runs at the HTTP layer, interceptors run later inside Nest).
- `customLogLevel` — without this, 4xx responses log as `info` and pollute error alerts. 5xx and uncaught errors log as `error`; 4xx as `warn`; everything else as `info`.
- `autoLogging.ignore` on `/health` — load balancer health checks fire continuously and dominate logs without this filter.
- `serializers + redact` — two-layer protection: serializers return empty objects so pino never serializes req/res at all; redact removes the now-empty keys. Without both layers, raw headers (including `Authorization`) can appear in logs.
- `customProps` injects `traceId` (set by `TraceIdInterceptor`) and `route` into every log line. `routeOptions?.url` returns the route template (`/users/:id`) instead of the resolved URL (`/users/123`), which is critical for log grouping and metrics.
- `customAttributeKeys: { responseTime: 'duration' }` — renames pino-http's `responseTime` field to `duration` for consistency with other observability tools.
- `singleLine: true` in pino-pretty — one log line per entry in dev; easier to read in terminal without losing structure.

---

## Joi env vars

Add to the validation schema in `app.module.ts`:

```typescript
LOG_LEVEL: Joi.string().valid('trace', 'debug', 'info', 'warn', 'error').default('info'),
```

---

## Completion gate

Before returning to `engineering-nestjs` Step 8: start the application and confirm it boots cleanly with `LOG_LEVEL` present in `.env.example`. Confirm that a request produces a structured log line with `traceId` and `route` fields. In development, confirm `pino-pretty` single-line output is visible in the terminal.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
