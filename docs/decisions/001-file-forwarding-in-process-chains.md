# ADR 001: File Forwarding in Process Chains

## Context

When one process calls another via `process.call`, the field mappings can reference files from the original trigger input (e.g. `input.email`). By the time the after step runs, `context["input"]` contains the already-resolved file dict — i.e. what came out of `resolve_file_refs()`. This dict has `content` (decoded text) and `path` (local filesystem path) added to it.

Naively forwarding this resolved dict over HTTP to the downstream process causes two problems:

1. **Encoding errors on servers with ASCII locale** (`LANG=C`, common in Docker). The decoded content may contain non-ASCII characters (e.g. German `ö`, `ü`). Even though httpx uses `ensure_ascii=False`, the Python process itself or logging handlers may have an ASCII default encoding, causing `UnicodeEncodeError`.

2. **Inflated payload size.** The decoded email text is duplicated in the HTTP body, even though the downstream process would re-read it from storage anyway.

## Decision

**Send clean FileRefs across process boundaries, never resolved content.**

Two changes enforce this:

1. **`files.py` — preserve `type` and `key` on resolved dicts.** After resolving a FileRef, the result dict retains `type: "local"` and `key: <storage key>`. This makes the resolved dict a valid FileRef so it can be re-resolved by a downstream process.

2. **`process_call.py` — strip `content` and `path` before forwarding.** In `_build_input()`, any value that looks like a local/s3/url FileRef (has `type` and `key`) has its `content` and `path` fields removed before being sent. The downstream process receives only the FileRef metadata and re-resolves the file from shared storage.

## Consequences

- Files must be in shared storage accessible to all processes on the same runtime. This is the case by default (single runtime, `LocalStorage`). For multi-host deployments, an S3 or shared filesystem storage backend is required anyway.
- Downstream processes always get fresh file resolution (re-read from storage, re-decoded), which is correct.
- The `content` field is never available in `context["input"]` inside after.py — only the raw file dict with FileRef fields. This is intentional; only execution.py needs `content`.

## Open Questions / TODOs

- The connector `file` → InputSchema field name aliasing (`_apply_connector_file_aliases` in `pipeline.py`) is a runtime workaround for a mismatch between connector output keys and generated InputSchema field names. A cleaner long-term solution would be to either:
  - Teach the agent (via `trigger-action-registry.md`) that email triggers always expose the `.eml` file at `input.file`, and have the agent generate field mappings with `input.file` as source.
  - Standardize connectors to always use the same key (e.g. always `file`), and keep generated InputSchemas consistent.
  - Current approach: runtime inspects the InputSchema at pipeline entry and adds an alias if a FileInput field with a different name is found and `file` is present in the input dict.
