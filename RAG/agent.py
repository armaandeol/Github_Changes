"""Agentic RAG orchestration loop.

Takes the three upstream artifacts (ML result, code review finding, PR context),
lets the model decide what engineering knowledge it needs, retrieves it on demand,
and terminates with a structured DeploymentReport produced via a tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

from openai import OpenAI
from pydantic import ValidationError

from schemas import AgentInput, DeploymentReport
from tools import (
    ALL_TOOLS,
    RETRIEVE_TOOL_NAME,
    SUBMIT_TOOL,
    SUBMIT_TOOL_NAME,
    KnowledgeBase,
)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
MAX_ITERATIONS = 6


AGENT_SYSTEM_PROMPT = """\
You are the risk-assessment agent for a deployment risk scoring system. You will be
given two pieces of evidence about a pull request:
  * ml_result — a machine-learning defect prediction: a JIT `feature_vector` (change
    size, entropy, developer experience, etc.) and a `shap_explanation` whose
    `output_value` is the bug probability in [0, 1] and whose `contributions` list the
    top features driving that prediction (each with a signed shap_value and a
    direction of increases_risk / decreases_risk).
  * code_review_finding — an LLM code review with a `risk_score`, `risk_level`, a
    prose `summary`, `top_risk_factors`, and `recommendations`.

Your job:
1. Analyze the two signals together. Look for compounding risk — e.g. a security
   concern raised in the review that lines up with a feature the ML model's SHAP
   contributions also flagged as risk-increasing is worse than either signal alone.
2. Decide what engineering guidance would make your assessment more defensible.
   Only retrieve what's actually relevant to THIS PR's specific findings — don't
   retrieve generically.
3. Use retrieve_engineering_knowledge to pull that guidance. You may call it
   multiple times across different concerns, but stop once additional retrieval
   would be redundant.
4. Once you have enough evidence, call submit_final_report exactly once with your
   complete assessment.

Deployment risk classification guidance (use judgment, these are not hard
thresholds): bug_probability alone rarely determines risk — a high probability with
no security/architecture concerns may be MEDIUM, while a moderate probability
combined with an unresolved security finding or a breaking API change should push
toward HIGH. Justify engineering_guidance and recommended_actions using what you
actually retrieved, not generic advice.

Carry bug_probability through to your report unchanged: it is ml_result's
shap_explanation.output_value. In sources_consulted, list only the knowledge base
domains you actually retrieved from.

You have a maximum of {max_tool_calls} tool calls before you must submit your final
report."""


# --------------------------------------------------------------------------- #
# Fallback parsing for routes that emit tool calls as plain text
# --------------------------------------------------------------------------- #


@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Function
    type: str = "function"


_KNOWN_TOOL_NAMES = (SUBMIT_TOOL_NAME, RETRIEVE_TOOL_NAME)


def _iter_json_objects(text: str) -> Iterable[dict[str, Any]]:
    """Yield every balanced top-level JSON object embedded in `text`."""
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(obj, dict):
                        yield obj
                start = -1


def try_parse_tool_call_from_text(content: str | None) -> _ToolCall | None:
    """Recover a tool call that arrived as JSON text instead of a `tool_calls` entry.

    Some Kimi routes (particularly third-party aggregators) occasionally emit the
    call inside `message.content`. Handles both the wrapper form
    `{"name": ..., "arguments": {...}}` and a bare arguments object.
    """
    if not content:
        return None

    # Strip markdown fences so ```json blocks parse.
    text = re.sub(r"```(?:json|tool_call)?", "", content)

    for obj in _iter_json_objects(text):
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        if isinstance(name, dict):  # {"function": {"name": ..., "arguments": ...}}
            inner = name
            name = inner.get("name")
            obj = {"name": name, "arguments": inner.get("arguments")}

        if isinstance(name, str) and name in _KNOWN_TOOL_NAMES:
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                args_str = args
            else:
                args_str = json.dumps(args or {})
            return _ToolCall(id="recovered_0", function=_Function(name=name, arguments=args_str))

        # Bare arguments object that structurally matches one of the tools.
        if "deployment_risk" in obj and "bug_probability" in obj:
            return _ToolCall(
                id="recovered_0",
                function=_Function(name=SUBMIT_TOOL_NAME, arguments=json.dumps(obj)),
            )
        if "query" in obj and "domain" in obj:
            return _ToolCall(
                id="recovered_0",
                function=_Function(name=RETRIEVE_TOOL_NAME, arguments=json.dumps(obj)),
            )

    return None


# --------------------------------------------------------------------------- #
# Run bookkeeping
# --------------------------------------------------------------------------- #


@dataclass
class ToolCallRecord:
    iteration: int
    name: str
    arguments: dict[str, Any]
    recovered_from_text: bool = False
    result_domains: list[str] = field(default_factory=list)


@dataclass
class AgentRunResult:
    report: DeploymentReport
    tool_calls: list[ToolCallRecord]
    iterations_used: int
    forced_finalization: bool
    domains_retrieved: list[str]

    @property
    def retrieval_call_count(self) -> int:
        return sum(1 for c in self.tool_calls if c.name == RETRIEVE_TOOL_NAME)


# --------------------------------------------------------------------------- #
# Message helpers
# --------------------------------------------------------------------------- #


def build_client() -> OpenAI:
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("KIMI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Export your API key before running."
        )
    
    base_url = os.environ.get("GROQ_BASE_URL")
    if not base_url:
        if os.environ.get("KIMI_API_KEY") and not os.environ.get("GROQ_API_KEY"):
            base_url = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
        else:
            base_url = DEFAULT_BASE_URL

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
    )


def _serialize_assistant_message(msg: Any) -> dict[str, Any]:
    """Convert an SDK assistant message into a plain dict safe to send back.

    Drops null fields the API rejects, and preserves `reasoning_content` verbatim —
    thinking-mode Kimi models require it on every subsequent turn of a multi-step
    tool exchange.
    """
    dumped = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
    out: dict[str, Any] = {"role": "assistant", "content": dumped.get("content") or ""}

    if dumped.get("tool_calls"):
        out["tool_calls"] = [
            {
                "id": tc["id"],
                "type": tc.get("type", "function"),
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                },
            }
            for tc in dumped["tool_calls"]
        ]

    reasoning = dumped.get("reasoning_content")
    if reasoning:
        out["reasoning_content"] = reasoning

    return out


def build_user_payload(agent_input: AgentInput) -> str:
    """Serialize the two inputs into the opening user turn."""
    payload = {
        "ml_result": agent_input.ml_result.model_dump(),
        "code_review_finding": agent_input.code_review_finding.model_dump(),
    }
    return json.dumps(payload, indent=2)


def _log(verbose: bool, message: str) -> None:
    if verbose:
        print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def run_agent(
    agent_input: AgentInput,
    *,
    max_iterations: int = MAX_ITERATIONS,
    client: OpenAI | None = None,
    model: str | None = None,
    kb: KnowledgeBase | None = None,
    verbose: bool = False,
) -> AgentRunResult:
    """Drive the agent to a validated DeploymentReport.

    Terminates either when the agent calls `submit_final_report`, or — if it burns
    through `max_iterations` first — via a forced finalization turn in which the
    retrieval tool is withdrawn and only `submit_final_report` remains available.
    """
    client = client or build_client()
    model = model or os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
    kb = kb or KnowledgeBase()

    system_prompt = AGENT_SYSTEM_PROMPT.format(max_tool_calls=max(max_iterations, 1))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_user_payload(agent_input)},
    ]

    tool_log: list[ToolCallRecord] = []
    iteration = 0

    for iteration in range(1, max_iterations + 1):
        _log(verbose, f"\n── iteration {iteration}/{max_iterations} ──")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=ALL_TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        messages.append(_serialize_assistant_message(msg))

        recovered = False
        calls = list(msg.tool_calls or [])
        if not calls:
            fallback = try_parse_tool_call_from_text(msg.content)
            if fallback is None:
                # No tool call and nothing recoverable — nudge once, then let the
                # forced-finalization branch below handle it.
                _log(verbose, "  model replied with plain text; nudging toward a tool call")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Respond only by calling a tool: either "
                            f"{RETRIEVE_TOOL_NAME} to gather more evidence, or "
                            f"{SUBMIT_TOOL_NAME} to finish."
                        ),
                    }
                )
                continue
            calls = [fallback]
            recovered = True
            # The recovered call was never a real tool_calls entry, so the assistant
            # turn we just appended cannot take a `role: tool` reply. Replace it with
            # the raw text to keep the transcript valid.
            messages[-1] = {"role": "assistant", "content": msg.content or ""}
            _log(verbose, "  recovered a tool call from plain text")

        tool_messages: list[dict[str, Any]] = []
        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                _log(verbose, f"  {name}: unparseable arguments ({exc})")
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            {"error": f"arguments were not valid JSON: {exc}"}
                        ),
                    }
                )
                continue

            if name == SUBMIT_TOOL_NAME:
                _log(verbose, "  → submit_final_report")
                try:
                    report = _finalize(agent_input, args, kb, verbose)
                except ValidationError as exc:
                    _log(verbose, f"  report failed validation, asking for a correction")
                    tool_log.append(ToolCallRecord(iteration, name, args, recovered))
                    if recovered:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "That report failed schema validation:\n"
                                    f"{exc}\nCall submit_final_report again with valid fields."
                                ),
                            }
                        )
                    else:
                        tool_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": json.dumps(
                                    {"error": "report failed validation", "detail": str(exc)}
                                ),
                            }
                        )
                    break
                tool_log.append(ToolCallRecord(iteration, name, args, recovered))
                return AgentRunResult(
                    report=report,
                    tool_calls=tool_log,
                    iterations_used=iteration,
                    forced_finalization=False,
                    domains_retrieved=list(kb.domains_retrieved),
                )

            if name == RETRIEVE_TOOL_NAME:
                query = args.get("query", "")
                domain = args.get("domain", "any")
                _log(verbose, f"  → retrieve[{domain}] {query!r}")
                result = kb.retrieve(query=query, domain=domain)
                hit_domains = [r["domain"] for r in result.get("results", [])]
                for r in result.get("results", []):
                    _log(
                        verbose,
                        f"     · {r['domain']} / {r['section']} (relevance {r['relevance']})",
                    )
                tool_log.append(
                    ToolCallRecord(iteration, name, args, recovered, hit_domains)
                )
                if recovered:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Retrieval result:\n{json.dumps(result)}",
                        }
                    )
                else:
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result),
                        }
                    )
                continue

            _log(verbose, f"  unknown tool {name!r}")
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps({"error": f"unknown tool '{name}'"}),
                }
            )

        messages.extend(tool_messages)

    # Loop exhausted without a final report — force the conclusion.
    _log(verbose, "\n── forced finalization (iteration budget exhausted) ──")
    report = _force_finalization(
        client=client,
        model=model,
        messages=messages,
        agent_input=agent_input,
        kb=kb,
        tool_log=tool_log,
        iteration=iteration,
        verbose=verbose,
    )
    return AgentRunResult(
        report=report,
        tool_calls=tool_log,
        iterations_used=max(iteration, 1),
        forced_finalization=True,
        domains_retrieved=list(kb.domains_retrieved),
    )


def _force_finalization(
    *,
    client: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    agent_input: AgentInput,
    kb: KnowledgeBase,
    tool_log: list[ToolCallRecord],
    iteration: int,
    verbose: bool,
    attempts: int = 2,
) -> DeploymentReport:
    """Re-prompt with only `submit_final_report` available so the agent must conclude."""
    messages = messages + [
        {
            "role": "user",
            "content": (
                "You have reached your tool call budget. No further retrieval is "
                "available. Call submit_final_report now with the evidence you have "
                "already gathered."
            ),
        }
    ]

    last_error: Exception | None = None
    for attempt in range(attempts):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=[SUBMIT_TOOL],
            tool_choice={"type": "function", "function": {"name": SUBMIT_TOOL_NAME}},
        )
        msg = response.choices[0].message
        messages.append(_serialize_assistant_message(msg))

        call = None
        for candidate in msg.tool_calls or []:
            if candidate.function.name == SUBMIT_TOOL_NAME:
                call = candidate
                break
        if call is None:
            call = try_parse_tool_call_from_text(msg.content)
            if call is not None:
                messages[-1] = {"role": "assistant", "content": msg.content or ""}

        if call is None or call.function.name != SUBMIT_TOOL_NAME:
            last_error = RuntimeError(
                "Forced finalization: model did not call submit_final_report."
            )
            _log(verbose, f"  attempt {attempt + 1}: no submit call, retrying")
            messages.append(
                {
                    "role": "user",
                    "content": "You must call submit_final_report. Do it now.",
                }
            )
            continue

        try:
            args = json.loads(call.function.arguments or "{}")
            report = _finalize(agent_input, args, kb, verbose)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            _log(verbose, f"  attempt {attempt + 1}: invalid report ({exc})")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That report was invalid:\n{exc}\n"
                        "Call submit_final_report again with valid fields."
                    ),
                }
            )
            continue

        tool_log.append(ToolCallRecord(iteration, SUBMIT_TOOL_NAME, args))
        return report

    raise RuntimeError(
        f"Agent failed to produce a valid DeploymentReport during forced "
        f"finalization after {attempts} attempts. Last error: {last_error}"
    )


def _finalize(
    agent_input: AgentInput,
    args: dict[str, Any],
    kb: KnowledgeBase,
    verbose: bool,
) -> DeploymentReport:
    """Validate the submitted arguments and reconcile the two auditable fields."""
    report = DeploymentReport(**args)

    # bug_probability is a passthrough of the ML result, not a judgment call — if the
    # model restated it inexactly, correct it rather than shipping a wrong number.
    ml_probability = agent_input.ml_result.bug_probability
    if abs(report.bug_probability - ml_probability) > 1e-3:
        _log(
            verbose,
            f"  corrected bug_probability {report.bug_probability} -> {ml_probability}",
        )
        report.bug_probability = ml_probability

    # sources_consulted must reflect what was actually read.
    if not report.sources_consulted and kb.domains_retrieved:
        _log(verbose, "  filled empty sources_consulted from actual retrievals")
        report.sources_consulted = list(kb.domains_retrieved)

    return report
