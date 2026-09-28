# TRV-C2-004 — Accommodation Plan Generation & Branding Agent

> **Category**: Cat 2 (domain workflow, human-in-the-loop)
> **Industry**: TRV

## Overview

Accommodation Plan Generation & Branding Agent drafts branded lodging plans for a property, using
that property's *own* history of past plans as the reference. It builds a structured profile from
the property description, retrieves the most relevant past plans, generates several candidate
drafts — title, promotional copy, included experiences, and a price range where the history
supports one — refines the tone, and then stops and waits for an operator to approve or send it
back with instructions. Approved plans can be written back so the next round is informed by what
was actually adopted.

It suits the recurring job of putting the next season's plans together: a property or a small
group that already has years of plan history in a reservation system, and wants drafts that sound
like their own plans rather than generic copy, with a person deciding what goes live.

Three things shape the design:

- **The history is the source of the price, and there is no substitute.** A price range is offered
  only when the property's own past plans support one; with no history the plans come back with
  the price deliberately absent and a note saying so. The agent does not infer a market rate.
- **Every read and write is tenant-scoped, at the service.** `tenant_id` is a required field on
  every request, all knowledge-base access goes through a single service that scopes each query,
  and the output gate additionally checks that the internal tenant identifier has not leaked into
  customer-facing plan text. A run without `tenant_id` is refused before anything is read.
- **A human is in the loop by design.** The run suspends with the refined drafts; the operator
  approves them or replies with instructions ("cheaper", "aim younger") and the workflow loops back
  to generation, up to `max_revisions`.

The agent produces drafts. It does not publish to a reservation system, manage inventory, optimise
revenue, or judge advertising-law compliance — final wording and the decision to publish stay with
a person.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- an LLM provider — `anthropic` (default) or `openai`, selected by `llm.provider`. The client is
  imported lazily, so only the configured provider needs to be installed, and the API key comes
  from the runtime secret store. There is **no offline fallback**: a missing or invalid provider
  fails at start-up rather than quietly degrading;
- the past-plan knowledge base. It is a two-layer store: a relational database as the system of
  record and a vector index derived from it, synchronised strictly one way (a confirmed database
  write updates the index, never the reverse). With neither injected the service falls back to an
  **in-process dictionary that is lost on restart** — fine for tests and a first look, not for use;
- **a persistent checkpoint saver.** The standalone server attaches an in-process `InMemorySaver`
  for both the outer graph and the inner workflow, so a suspended review does not survive a restart
  and is invisible to a second replica;
- caller authentication in front of the entry adapter.

Runtime parameters in `config/config.yaml`:

| Key | Meaning |
|---|---|
| `llm.provider` (`anthropic`) | `anthropic` or `openai`. An unset or unrecognised value is a start-up error |
| `hitl.enabled` (`true`) / `hitl.max_hitl` (`8`) / `memory_enabled` (`true`) | The review loop. Checkpointing is mandatory with HITL — `hitl.enabled: true` without `memory_enabled: true` fails at compile time |
| `max_revisions` (`8`) | Ceiling on revision rounds |
| `kb.top_k` (`5`) | How many similar past plans are retrieved per generation |
| `max_retry` (`3`), `timeout_seconds` (`30`) | Framework-level runtime settings |

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is built for the platform and connects to it at start-up. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The agent does not fall back to a degraded mode when the framework is missing: `src/api/server.py`
imports `framework.*` and `shared.*` at module scope and builds, compiles and provisions the graph
at import time, so without the AgentCore package the process fails to start — and so does an
unrecognised `llm.provider`, by design. With the framework installed but no platform around it the
server starts and answers `/health`, backed by the in-memory knowledge-base fallback, but it
configures no caller authentication, so `/invoke` answers **401** until `INVOKE_AUTH_TOKEN` is set
or verifying middleware is placed in front, and no API key is provisioned, so the first request
that reaches the model fails. The test suite runs without a platform connection.

## Input

`POST /invoke` takes `input` — a JSON string — plus an optional `session_id`. `input_context` values
are merged in as defaults, so a platform caller can pass `tenant_id` out of band. Plain text that
is not JSON is accepted as a shorthand for generate mode with that text as the property
description.

Callers must be authenticated: a verified trust level from upstream middleware, or an
`Authorization: Bearer` header matching `INVOKE_AUTH_TOKEN` when that is set. Anything else is
**401**.

`tenant_id` is required in every mode — it is the knowledge-base isolation key, and a request
without it is refused. Three modes:

| Mode | Payload | Effect |
|---|---|---|
| `ingest` | `csv_payload` — CSV exported from the reservation system | Bulk-loads past plans into the knowledge base |
| `generate` (default) | `facility_text` **or** `facility_url` (http/https only), plus optional `season`, `target_segment` … | Produces branded drafts and enters the review loop |
| `feedback` | `adopted_plan` (object) | Records that a plan was adopted, so later generations reflect it |

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{"session_id":"plan-001","input":"{\"mode\":\"generate\",\"tenant_id\":\"hotel-001\",\"request_params\":{\"facility_text\":\"Seaside hot-spring inn, every room ocean-facing.\",\"season\":\"summer\",\"target_segment\":\"couples\"}}"}'
```

A generate run comes back suspended for review, carrying a `thread_id` and the `session_id` to echo
back. Continue it with `POST /resume`:

```bash
curl -X POST http://127.0.0.1:8000/resume \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{"thread_id":"<from /invoke>","feedback":"approve","session_id":"plan-001"}'
```

`feedback` is `"approve"` (or `{"action": "approve"}`) to accept; anything else — for example
`{"instructions": "make it cheaper"}` — loops a revision. Resume fails closed: an unknown thread is
**404**, a `session_id` that does not own it is **403**, and a thread that is not actually awaiting
input is **409**. Concurrent resumes on one thread are serialised within the process.

**The ownership model has a stated limit.** `session_id` is the primary owner key. Under a single
shared `INVOKE_AUTH_TOKEN` every token caller has an empty `caller_id`, so the check stops a caller
who does *not* know the victim's `session_id`, but not one who shares the token and knows both
values. A real deployment closes this automatically: platform middleware stamps a verified
`caller_id`, which the check then enforces. The token-only path is for single-tenant verification,
not multi-tenant production. Likewise, the per-thread lock is single-process — a durable
multi-process deployment needs a lease or compare-and-set in the shared checkpoint store.

## Output

The standard AgentCore envelope is returned; `result` is shaped by mode. Generate mode carries
`plans`, `plan_count`, `revision_rounds` and a `price_note` that states plainly whether the price
range came from real history or is absent for want of it. Ingest and feedback modes return their
own result objects. Every response carries `template_id` and `mode`.

Values depend on the property description and on the history the knowledge base holds.

## Security and Limitations

Every node requires verified-external trust. Tenant isolation is enforced in the knowledge-base
service, so no query crosses properties, and the output gate rejects a response whose plan text
contains the internal tenant identifier. Request size is capped at the input gate on top of the
framework's own scan.

Known limits, recorded rather than open:

- **Without a real store, nothing persists.** The in-memory fallback loses the whole knowledge base
  on restart, and the in-process checkpoint saver loses any suspended review with it.
- **URL ingestion handles public pages only.** Redirects and internal URLs are not supported — pass
  the text directly when a page cannot be fetched.
- **Price ranges need history.** Until a property has ingested past plans, drafts come back without
  a price, on purpose.
- **A provider failure is an error, not a silent gap.** The profile and generation steps report the
  exception type and a truncated message rather than returning empty; refinement is the exception —
  it degrades to the unrefined drafts, because losing the drafts would be worse than losing the
  polish. Provider messages are truncated before they are recorded, since they can echo the request
  and the error channel is checkpointed.
- **Compliance is out of scope.** Advertising-law and other regulatory review of the copy remains a
  human step before publication.

Output is machine-generated and assistive. The operator review in the loop is part of the design,
not a formality.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2) — an outer pipeline whose main step wraps an
  inner generation-and-refinement workflow with retrieval over past plans
- Generation mode: `llm`
- HITL: **enabled** — an operator approves or revises the drafts (`max_hitl` / `max_revisions`: 8)
- Persistence: checkpointing required and enabled, plus the past-plan knowledge base. The
  standalone server uses in-process stand-ins for both; production injects durable ones
- Secrets: the configured provider's API key, from the runtime secret store; the entry adapter
  additionally reads `INVOKE_AUTH_TOKEN` from the environment when caller-token authentication is
  in use
- External API clients: imported lazily per configured provider; the knowledge-base backends are
  deployment-injected

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, without a language model and without external storage,
using fakes and the in-memory fallback — including the suspend-and-resume path.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the knowledge-base contract, the HITL
loop, the LLM-failure decision record and the security-layer mapping. `docs/03_test_spec.md`
records the test specification and results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
