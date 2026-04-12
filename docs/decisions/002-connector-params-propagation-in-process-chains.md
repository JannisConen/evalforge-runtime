# ADR 002: Connector Params Propagation in Process Chains

## Context

When a scheduled/connector-triggered process (e.g. `email-kategorisierung`, triggered by Gmail) calls a downstream process via `process.call` (e.g. `antwort-auf-support-anfragen`, triggered by webhook), the downstream process needs to perform email actions — reply, forward, send — on behalf of the originating mailbox.

The problem: email actions use `get_email_connector()` in `actions/base.py`, which reads `context["initiator"]`. In `pipeline.py`, `context["initiator"]` is set to `proc_config.connector_params` — the connector config from `evalforge.config.yaml` for that specific process. For webhook-triggered processes, `connector_params` is `None`, so `initiator` is `{}`, and `GmailConnector` defaults `mailbox` to `"me"`. With a service account (which requires domain-wide delegation and an explicit mailbox), this raises:

```
Gmail service account requires explicit 'mailbox' parameter for delegation
```

More broadly: any downstream process in a chain that needs to act on behalf of the original connector (send email, upload to Drive, post to Slack) would fail because it has no connector context.

## Decision

**Propagate two pieces of trigger context through the process chain via HTTP headers.**

### Problem 1: missing mailbox / connector identity

Four changes:

1. **`process_call.py`**: When `context["initiator"]` is non-empty, serialize it as JSON and send it in an `X-Connector-Params` header alongside the existing `X-Request-ID` header.

2. **`server.py`**: Read and parse `x-connector-params` from the incoming request headers. Pass it as `initiator=` to `pipeline.execute_process()`.

3. **`pipeline.py`**: `execute_process()` accepts an optional `initiator` kwarg. When building `context["initiator"]` for the after step, use `proc_config.connector_params or initiator or {}`. The process's own config takes priority; the propagated value is a fallback.

### Problem 2: wrong trigger.ref (message ID)

The `email.reply` and `email.forward` actions use `trigger.ref` as the message ID to look up in the email provider. In a process chain, the downstream process's `TriggerContext` is constructed in `server.py` with `ref=execution_id` (a UUID), not the original Gmail message ID. The Gmail API returns 400 Bad Request when passed a UUID.

4. **`process_call.py`**: Forward `trigger.ref` from the caller's TriggerContext as an `X-Trigger-Ref` header. The `trigger` arg is available directly in `ProcessCallAction.run()`.

5. **`server.py`**: Read `x-trigger-ref` and use it as `trigger.ref` when constructing `TriggerContext`. Falls back to `execution_id` for standalone webhook calls so behavior is unchanged.

## Why a header, not a body field

The input body is typed and validated against the target's `InputSchema`. Injecting extra fields would either fail validation or pollute the input model. Headers are a clean out-of-band channel for runtime metadata, consistent with how `X-Request-ID` is propagated.

## Consequences

- **Transitive propagation**: If process A calls B which calls C, C inherits A's connector params and trigger ref. Each hop forwards what it received (or its own if it has one).
- **Caller config wins**: If a downstream process has its own connector config, its own `connector_params` takes precedence over the propagated value. Propagation is a fallback only.
- **Trigger ref semantics**: In a chain, `trigger.ref` in downstream processes refers to the *original* message, not the current execution. This is intentional — `email.reply` should reply to the original email, not a synthetic ID.
- **Scope**: Only affects `process.call` chains. Direct webhook calls have no propagation headers and behave as before.
- **Security**: Headers are only sent between processes on the same runtime (same `RUNTIME_BASE_URL`, authenticated with `EVALFORGE_SERVICE_KEY`). Not exposed to external callers.

## Alternatives Considered

- **Embed connector params in the process call body**: Rejected — would pollute the typed input schema and require every downstream InputSchema to accept them.
- **Require explicit connector config on every process**: Rejected — forces duplication of mailbox/auth config across every process in a chain, and breaks whenever the upstream mailbox changes.
- **Store initiator in DB and look up by request_id**: Rejected — too complex for a stateless HTTP call, and `request_id` is for tracing, not config.
