---
name: engineering-kafka
description: "Kafka integration rules: event contracts, producer rules, consumer idempotency, DLQ handling, and schema evolution. Use when implementing Kafka producers or consumers, defining event schemas, or handling messaging errors."
version: 2.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Kafka

## Event contracts

- Event schemas are Public Contracts. Any schema change requires compatibility analysis per the `engineering-architecture` skill (compatibility governance section).
- Every event must be registered in `registries/events.md` with topic, payload shape, producers, and consumers before being produced or consumed.
- Never produce an event whose schema is not registered.

---

## Producer rules

- Producers are responsible for message serialization and partition key selection.
- Use a consistent partition key strategy (entity ID) to maintain ordering within a partition.
- Enable idempotent producer config (`enable.idempotence=true`) to prevent duplicate messages on retry.
- Log the event name, key, and partition before producing. Log confirmation or error after.

---

## Consumer rules

- Every consumer must be idempotent — messages may be redelivered after broker restart or rebalance.
- Use a consumer group ID specific to the service and purpose: `<service>.<feature>`.
- Commit offsets only after successful processing — never before.
- Never block the consumer thread. Offload processing to async handlers.
- Log the event name and key at the start of each handler. Log outcome (success/failure) after.

---

## Error handling

- **Transient errors** (network timeout, temporary DB unavailability): retry with backoff, then send to DLQ after max attempts.
- **Poison pill messages** (deserialization failure, schema mismatch): send to DLQ immediately. Do not retry. Log full message content for investigation.
- DLQ topic naming convention: `<original-topic>.dlq`.
- DLQ messages must include: original topic, partition, offset, error reason, timestamp.

---

## Schema evolution

- Adding optional fields: backward compatible — allowed.
- Removing a field consumed by known consumers: breaking — coordinate migration across all consumers before removing.
- Changing field type: breaking — version the schema or topic.
- Renaming a field: breaking — treat as add + remove.
- Use a schema registry (Confluent Schema Registry or equivalent) for production systems with multiple consumers.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
