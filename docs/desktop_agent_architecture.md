# Desktop Agent Architecture — D0 Contract

Status: accepted for incremental implementation
Scope: Research Copilot application; RAG-Anything remains an external service.

## Decision

The product is split into four explicit runtime planes:

1. **Electron Workbench** — React renderer, secure preload API, local settings,
   approval UI, and desktop lifecycle.
2. **Local Execution Plane** — MCP host and isolated local tool processes. It is
   the only plane allowed to operate local files, scripts, or third-party apps.
3. **Remote Agent Core** — threads, durable runs, orchestration, memory,
   artifacts, model routing, quality gates, and event streaming.
4. **Remote RAG Service** — retrieval and evidence metadata behind an adapter;
   its database is never packaged into Electron.

The current web application remains usable while these boundaries are added.
Electron is a client of the same versioned contracts, not a second backend.

## Aggregate boundaries

| Aggregate | Owns | Must not own |
| --- | --- | --- |
| Project | research scope, knowledge-base binding, members | chat transcript |
| Thread | user-visible conversation and branch ancestry | execution process |
| Run | one immutable execution attempt and its state | mutable transcript history |
| RunEvent | append-only fact emitted by a run | current UI projection |
| ToolCall | normalized built-in or MCP invocation | renderer-side execution |
| Approval | risk decision and audit record | tool implementation |
| Artifact | versioned brief, matrix, outline, survey, references | transient stream buffers |
| Memory | durable facts and compacted context projections | source transcript deletion |

## Run state machine

```text
queued -> running/preparing -> planning -> memory -> retrieving
       -> waiting_approval -> executing_tool -> generating -> verifying
       -> memory_update -> completed

Any active state -> failed | cancelled
failed -> a new retry Run linked by retry_of_run_id
```

State transitions belong to application code. A model may propose the next
action, but cannot directly mutate run state or grant tool permission.

## Event envelope v1

Every persisted event has this stable envelope:

```json
{
  "id": "event-...",
  "run_id": "run-...",
  "seq": 17,
  "event_type": "tool.completed",
  "stage": "executing_tool",
  "payload": {},
  "created_at": 1785686400.0
}
```

`(run_id, seq)` is unique. Events are append-only and replayed in sequence.
Existing SSE names such as `thinking`, `tool_call`, and `text_delta` are stored
unchanged during the compatibility phase. Canonical dotted event names are
introduced through an explicit versioned mapper, never by silently renaming
historical events.

High-frequency `text_delta` frames remain transport-only in the SQLite phase to
avoid one transaction per token. Completed or partial assistant content is
durable in the message projection. A future queue-backed event store may batch
model deltas without changing the semantic event contract.

The `turn_runs.trace_json` and `messages.trace_json` columns remain projections
for existing clients. `run_events` is the durable source for replay and future
desktop recovery.

## Context and memory

Three stores remain separate:

- Full transcript and run events: append-only audit history.
- Context projection: the bounded model view, which may be compacted.
- Durable memory: user/project facts with provenance and explicit deletion.

Compaction changes only the model projection. It never deletes transcript or
run-event history.

## Tool contract

Built-in retrieval and external MCP tools are normalized to one logical shape:

```text
ToolSpec { id, source, input_schema, risk, timeout, concurrency, annotations }
ToolCall { id, run_id, spec_id, input, status, executor, result, error }
```

Risk is one of `read_only`, `write`, `destructive`, or `open_world`.
Permission precedence is managed policy > project > user > session; deny wins.
The remote core sends an intent to the Electron host. Only the local host can
approve and execute a local MCP tool.

## Security boundaries

- Renderer code has no direct Node.js or operating-system access.
- Electron exposes an allow-listed, typed preload API with context isolation.
- Secrets live in OS credential storage, never renderer localStorage.
- Local MCP servers run as supervised child processes with timeout, output
  limits, reconnect policy, and process cleanup.
- Destructive and open-world operations require an auditable approval.
- Client errors are sanitized; provider details and local absolute paths remain
  server-side.

## Compatibility and rollout

1. Add durable run events while keeping current trace projections.
2. Rebuild the web timeline from run events and test reconnect/reload.
3. Introduce the Electron shell against the same HTTP/SSE contracts.
4. Add local MCP execution and approvals behind feature flags.
5. Extract the RAG adapter into a separately deployable service without
   modifying the RAG-Anything core.

Each step is independently releasable and reversible.
