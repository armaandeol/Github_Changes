# Agentic RAG Engine — Deployment Risk Scorer

The final merge stage of the Deployment Risk Scorer. It receives two artifacts from
upstream pipelines, autonomously retrieves whatever engineering guidance it decides it
needs, and emits a single validated `DeploymentReport`.

```
MLResult ─────────────┐
                      ├──▶  Agent loop  ◀──▶  retrieve_engineering_knowledge  ──▶  Chroma KB
CodeReviewFinding ────┘          │
                                 └──▶  submit_final_report  ──▶  DeploymentReport (JSON)
```

The two upstream pipelines (JIT features → XGBoost → SHAP, and the per-file LLM code
review) are **out of scope here** — their outputs arrive as JSON and are mocked by the
fixtures in [fixtures/](fixtures/). `MLResult` bundles the JIT `feature_vector` with the
SHAP explanation (whose `output_value` is the bug probability); `CodeReviewFinding` is the
reviewer's `llm_response` block (`risk_score`, `risk_level`, `summary`, `top_risk_factors`,
`recommendations`).

## What makes it agentic

The model is not on a fixed retrieval schedule. Each turn it decides whether it has
enough evidence:

- **The low-risk fixture** (docs-only change, `output_value` 0.07, `risk_level` Low)
  typically submits with **zero retrievals**.
- **PR #142** (auth change + SQL injection + breaking API) fans out across several
  rounds — OWASP for injection, feature flags for the auth rollout, API versioning for
  the breaking change — before concluding.

The budget is enforced in code (`max_iterations`), not left to the model's self-restraint.
Structured output is a **tool call**, so the report is schema-validated rather than parsed
out of free text.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Environment variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `KIMI_API_KEY` | **yes** | — | Your Moonshot API key. |
| `KIMI_BASE_URL` | no | `https://api.moonshot.cn/v1` | Use `https://api.moonshot.ai/v1` on an international account. |
| `KIMI_MODEL` | no | `kimi-k2-0711-preview` | Any Moonshot chat model with tool calling. |

```bash
export KIMI_API_KEY="sk-..."
# export KIMI_BASE_URL="https://api.moonshot.ai/v1"   # international account
```

Kimi is called through the `openai` Python SDK pointed at Moonshot's OpenAI-compatible
endpoint — its tool calling follows the standard function-calling format, so no separate
SDK is needed.

## Usage

**1. Build the knowledge base** (once; ~80 MB embedding model downloads on first run):

```bash
python ingest.py           # chunks knowledge_base/*.md into ./chroma_kb
python ingest.py --stats   # show what is indexed
```

Chroma's default local embedding function is used, so no embedding API key is required.
Re-running `ingest.py` rebuilds the collection from scratch rather than duplicating it.

**2. Run a report:**

```bash
python run_report.py fixtures/example_pr_142.json
python run_report.py fixtures/example_pr_142.json --verbose        # log every tool call
python run_report.py fixtures/example_pr_142.json --max-iterations 2
```

`--verbose` logs each retrieval query, the domain filter, and every chunk returned with
its relevance score (to stderr, so the JSON report on stdout stays pipeable).

## Tests

```bash
python -m pytest              # 19 offline tests, no API key needed
python -m pytest -m live      # additionally hits the real Kimi API
```

Offline tests drive the loop with a scripted fake client (`tests/fake_kimi.py`), so loop
mechanics are deterministic and free to run. They cover the three required scenarios —
high-risk PR → `HIGH` with multiple retrievals and non-empty `sources_consulted`;
low-risk PR → `LOW`/`MEDIUM` with ≤1 retrieval; exhausted budget → forced finalization —
plus the fallback parser, schema-invalid submissions, and the retrieval layer. The `live`
marker runs the same three scenarios against the real endpoint and is skipped unless
`KIMI_API_KEY` is set.

## Files

| File | Role |
| --- | --- |
| [schemas.py](schemas.py) | Pydantic contracts for both inputs and the output. |
| [knowledge_base/](knowledge_base/) | Seven original seed documents (OWASP, Google eng practices, MS secure coding, deployment, feature flags, API versioning, DB migration). |
| [ingest.py](ingest.py) | Paragraph-level chunking → Chroma collection, tagged with `{"domain": ...}`. |
| [tools.py](tools.py) | Tool schemas + the `KnowledgeBase` retriever. |
| [agent.py](agent.py) | The orchestration loop. |
| [run_report.py](run_report.py) | CLI entry point. |

## Implementation notes

**The knowledge base is a seed corpus.** The seven documents are original summaries
written for this demo, not reproductions of the published standards they are named after.
Swap in real ingested sources for production use.

**`submit_final_report`'s schema is generated**, not hand-written — it comes from
`DeploymentReport.model_json_schema()` (`tools.build_submit_tool`), so the tool contract
cannot drift from the Pydantic model.

**Two fields are reconciled after submission** (`agent._finalize`):
- `bug_probability` is a passthrough of the ML result, not a judgment call. If the model
  restates it inexactly it is corrected to the input value, and the correction is logged
  under `--verbose`.
- `sources_consulted` must reflect what was actually read, so if the model leaves it empty
  after retrieving, it is filled from the retriever's own record. Everything else in the
  report is the agent's own output.

**Robustness in the loop:**
- A schema-invalid report is returned to the model as a tool error for correction rather
  than crashing the run.
- Plain text with no tool call costs one iteration and earns a nudge, rather than an
  immediate failure.
- If the iteration budget is exhausted, the retrieval tool is **withdrawn** and the model
  is re-prompted with only `submit_final_report` available (and `tool_choice` pinned to
  it). If it still fails to produce a valid report after two attempts, a `RuntimeError` is
  raised — the run never silently returns `None`.

### Kimi-specific handling

1. **Tool calls arriving as text.** Some Kimi routes occasionally emit a tool call as JSON
   inside `message.content` instead of a real `tool_calls` entry.
   `try_parse_tool_call_from_text` recovers it, handling fenced code blocks, the
   `{"name", "arguments"}` wrapper form, and bare argument objects. When a call is
   recovered this way, its result is fed back as a `user` message rather than a `tool`
   message — there is no `tool_call_id` to reply to, and sending one would be rejected.
2. **`tool_choice="auto"`** is used in the main loop. `"required"` is deliberately not
   relied on to force a call, since behavior varies by model version; the text fallback is
   the actual safety net. `tool_choice` *is* pinned during forced finalization, where a
   specific call is genuinely required.
3. **`reasoning_content`** is preserved verbatim on assistant turns
   (`_serialize_assistant_message`). The default `kimi-k2-0711-preview` has no thinking
   mode, but if you swap in a thinking variant (K2.5/K2.6/K3), dropping this field breaks
   multi-step tool exchanges.
4. **Assistant messages are re-serialized** rather than passed back as a raw
   `model_dump()`, so SDK-populated null fields don't reach the API.
