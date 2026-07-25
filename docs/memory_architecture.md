# Conversation Memory Architecture

The chat memory system follows the same separation of concerns used by mature
agent products: the transcript remains the source of truth, short-term context
is bounded, summaries are versioned, and durable memories are explicit,
inspectable, and removable.

## Design goals

- Resolve follow-up references before retrieval, so questions such as “它后来呢？”
  become standalone search queries.
- Keep prompt size bounded without losing the thread's working state.
- Persist only useful preferences, goals, and decisions as long-term memory.
- Never let memory persistence failure break a valid chat response.
- Give users controls to inspect, disable, branch, or forget memory.

## Runtime flow

```text
user message
  -> load settings + recent transcript + latest summary + durable memories
  -> detect topic shift and rewrite follow-up into a standalone query
  -> retrieve with standalone query
  -> answer with original wording + bounded conversational context
  -> persist assistant response
  -> update thread state / extract safe explicit memories / compact if needed
  -> persist memory trace events
```

Retrieval and answer generation intentionally receive different inputs. The
retriever gets the disambiguated standalone query; the model still sees the
original question, recent turns, the thread summary, and selected durable
memories. This avoids both fragmented retrieval and unnatural restatement in
the final answer.

## Storage model

All tables live in `data/feedback.db` so conversation and memory writes share a
single durable SQLite boundary.

| Table | Responsibility |
| --- | --- |
| `memory_turns` | Per-turn lifecycle, rewritten query, status, and references |
| `thread_state` | Current topic and lightweight working state |
| `thread_summaries` | Immutable, versioned compaction snapshots |
| `durable_memories` | User preferences, goals, and decisions with provenance |
| `conversation_memory_settings` | Per-conversation use/generate switches |
| `conversation_lineage` | Parent-child relationship for forked conversations |

The transcript is never overwritten by compaction. A summary records message
bounds and a version, which keeps provenance and allows future re-summarization.

## Context policy

The prompt context is assembled in this order:

1. memory policy and safety instructions;
2. versioned thread summary;
3. current thread state;
4. up to `MEMORY_MAX_DURABLE_ITEMS` relevant durable memories;
5. the latest `MEMORY_RECENT_MESSAGES` normalized transcript messages;
6. the current user question.

Identical adjacent messages are normalized out. Topic shifts prevent stale
context from being forced onto a new question. Deterministic rewriting is the
default fast path; an LLM fallback can be enabled when provider latency allows.

## Safety and failure boundaries

- Extraction is conservative and limited to explicit preferences, goals, and
  decisions; content resembling credentials or secrets is rejected.
- Users can independently disable reading memory and generating new memory.
- Forgetting marks a durable memory inactive instead of erasing audit history.
- Memory finalization is isolated after answer persistence. If it fails, the
  answer remains available and the failure is logged.
- Every memory lifecycle step is emitted as a trace event for debugging.

## Public API

- `GET /api/conversations/{id}/memory` — inspect thread state, summary, and settings
- `PATCH /api/conversations/{id}/memory` — toggle memory use or generation
- `GET /api/memories` — list durable memories for the active knowledge base
- `DELETE /api/memories/{id}` — forget a durable memory
- `POST /api/conversations/{id}/fork` — create a transcript branch with lineage

## Operational tuning

The defaults are intentionally conservative: eight recent messages, compaction
after sixteen messages or 24,000 characters, five durable memories per turn,
and deterministic query rewriting. These limits can be changed through the
environment variables documented in `.env.example`.

