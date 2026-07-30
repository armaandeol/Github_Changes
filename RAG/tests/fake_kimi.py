"""A scripted stand-in for Kimi's chat completions endpoint.

Lets the orchestration loop — retrieval rounds, forced finalization, the plain-text
tool-call fallback — be tested deterministically and without network access. The
live-API equivalents of these tests live in test_agent.py behind the `live` marker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from tools import RETRIEVE_TOOL_NAME, SUBMIT_TOOL_NAME


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None
    role: str = "assistant"

    def model_dump(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (self.tool_calls or [])
            ]
            or None,
        }


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]


def tool_call_message(name: str, arguments: dict[str, Any], call_id: str = "call_0") -> FakeMessage:
    return FakeMessage(
        content="",
        tool_calls=[FakeToolCall(id=call_id, function=FakeFunction(name, json.dumps(arguments)))],
    )


def retrieve_message(query: str, domain: str, call_id: str = "call_0") -> FakeMessage:
    return tool_call_message(RETRIEVE_TOOL_NAME, {"query": query, "domain": domain}, call_id)


def submit_message(report: dict[str, Any], call_id: str = "call_submit") -> FakeMessage:
    return tool_call_message(SUBMIT_TOOL_NAME, report, call_id)


@dataclass
class FakeCompletions:
    script: list[Callable[[dict[str, Any]], FakeMessage] | FakeMessage]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("FakeKimiClient ran out of scripted responses")
        step = self.script.pop(0)
        message = step(kwargs) if callable(step) else step
        return FakeResponse(choices=[FakeChoice(message=message)])


@dataclass
class FakeChat:
    completions: FakeCompletions


class FakeKimiClient:
    """Replays a scripted list of assistant messages, one per `create` call."""

    def __init__(self, script: list[Any]) -> None:
        self.chat = FakeChat(completions=FakeCompletions(script=list(script)))

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


HIGH_RISK_REPORT: dict[str, Any] = {
    "deployment_risk": "HIGH",
    "bug_probability": 0.84,
    "ml_explanation": [
        "Number of files changed (nf) is the largest positive SHAP contributor (+0.74).",
        "Lines added (la, +0.51) and change entropy (ent, +0.43) compound the risk.",
        "Author experience (aexp, -0.29) offsets only marginally.",
    ],
    "code_review_findings": [
        "Possible SQL injection in the session lookup and login query.",
        "Authentication logic changed, including a legacy fallback path.",
        "Breaking API change in user_controller.get_user.",
    ],
    "engineering_guidance": [
        "Parameter binding, not escaping, is the primary injection defense.",
        "Gate security-relevant changes behind a flag so rollback is a config change.",
        "Breaking API changes require an explicit version and deprecation window.",
    ],
    "recommended_actions": [
        "Convert both raw queries to parameterized statements.",
        "Add integration tests for the auth flow before merging.",
        "Deploy behind a feature flag with a canary on login success rate.",
    ],
    "sources_consulted": ["owasp_security", "feature_flags", "api_versioning"],
}

LOW_RISK_REPORT: dict[str, Any] = {
    "deployment_risk": "LOW",
    "bug_probability": 0.07,
    "ml_explanation": [
        "The dominant SHAP contributions (nf, la, ent) are all negative; the change is small and documentation only.",
    ],
    "code_review_findings": [],
    "engineering_guidance": [
        "Small, self-contained changes are the lowest-risk unit of deployment.",
    ],
    "recommended_actions": ["Merge and deploy on the normal cadence."],
    "sources_consulted": [],
}
