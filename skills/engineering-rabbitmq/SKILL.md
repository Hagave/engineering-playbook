---
name: engineering-rabbitmq
description: "RabbitMQ integration rules: exchange routing, manual acknowledgment, DLQ, retry policies, and schema evolution. Use when implementing RabbitMQ publishers or consumers, defining exchanges and queues, or handling message errors."
version: 2.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# RabbitMQ

## Exchange and routing

- Define exchange type by routing requirement: `direct` (exact key match), `topic` (pattern match), `fanout` (broadcast to all bindings).
- Never publish directly to a queue — always publish to a named exchange.
- Routing keys and exchange names are Public Contracts. Register in `registries/events.md`.
- Declare exchanges and queues as durable.

---

## Consumer rules

- Consumer idempotency and the `<service>.<feature>` naming convention follow the same rules as the `engineering-kafka` skill — here redelivery is triggered by connection loss or channel failure.
- Use manual acknowledgment mode. Never use auto-ack in production.
- Acknowledge (`ack`) only after successful processing.
- Set prefetch count (`basicQos`) to limit in-flight messages per consumer and prevent memory exhaustion.

---

## Retry and DLQ

- The retry/DLQ policy (max attempts, final DLQ for manual inspection, required metadata, never drop silently) follows the same rules as the `engineering-kafka` skill. RabbitMQ transport specifics:
- NACK without requeue routes the message to the dead-letter exchange.
- Implement retry via dead-letter exchange with TTL: message returns to the original queue after a backoff delay.
- DLQ naming convention: `<original-queue>.dlq`.
- DLQ message metadata travels via message headers: original exchange, routing key, error reason, retry count, timestamp.

---

## Error handling

- Deserialization failure: NACK without requeue → DLQ immediately.
- Transient processing error: NACK with requeue (with backoff) or route to retry exchange.
- Never silently drop a message. Every rejection must be logged with reason.

---

## Schema evolution

Follow the same rules as the `engineering-kafka` skill (schema evolution section). Exchange and routing key changes are breaking — coordinate all publishers and consumers before changing.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
