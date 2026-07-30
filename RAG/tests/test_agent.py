"""Tests for the agentic RAG engine.

Two layers:
  * Deterministic tests drive the loop with a scripted fake Kimi client, so loop
    mechanics (retrieval rounds, forced finalization, the plain-text fallback) are
    verified offline and without burning API credits.
  * Live tests exercise the same three scenarios against the real Kimi endpoint;
    they are skipped unless KIMI_API_KEY is set. Run them with `-m live`.
"""

from __future__ import annotations

import json
import os

import pytest

from agent import build_client, run_agent, try_parse_tool_call_from_text
from fake_kimi import (
    HIGH_RISK_REPORT,
    LOW_RISK_REPORT,
    FakeKimiClient,
    FakeMessage,
    retrieve_message,
    submit_message,
)
from schemas import DeploymentReport
from tools import RETRIEVE_TOOL_NAME, SUBMIT_TOOL_NAME

live = pytest.mark.live
requires_key = pytest.mark.skipif(
    not os.environ.get("KIMI_API_KEY"), reason="KIMI_API_KEY not set"
)


# --------------------------------------------------------------------------- #
# 1. High-risk PR #142: multiple retrievals, HIGH verdict, auditable sources
# --------------------------------------------------------------------------- #


def test_high_risk_pr_produces_high_report(high_risk_input, kb):
    client = FakeKimiClient(
        [
            retrieve_message("SQL injection prevention", "owasp_security", "c1"),
            retrieve_message("feature flag for auth changes", "feature_flags", "c2"),
            retrieve_message("breaking API change rollout", "api_versioning", "c3"),
            submit_message(HIGH_RISK_REPORT),
        ]
    )

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb, max_iterations=6)

    assert isinstance(result.report, DeploymentReport)
    assert result.report.deployment_risk == "HIGH"
    assert result.report.bug_probability == pytest.approx(0.84)
    assert result.report.sources_consulted, "report must record what it read"
    assert result.retrieval_call_count > 1, "high-risk PR should trigger several retrievals"
    assert not result.forced_finalization
    # Retrieval actually hit the knowledge base rather than returning nothing.
    assert "owasp_security" in result.domains_retrieved


@live
@requires_key
def test_high_risk_pr_live(high_risk_input, kb):
    from tools import KnowledgeBase  # noqa: F401  (kb fixture already provides one)

    result = run_agent(high_risk_input, client=build_client(), kb=kb, verbose=True)

    assert isinstance(result.report, DeploymentReport)
    assert result.report.deployment_risk == "HIGH"
    assert result.report.sources_consulted
    assert result.retrieval_call_count >= 1


# --------------------------------------------------------------------------- #
# 2. Low-risk PR: terminates cleanly, LOW/MEDIUM, minimal retrieval
# --------------------------------------------------------------------------- #


def test_low_risk_pr_terminates_with_minimal_retrieval(low_risk_input, kb):
    client = FakeKimiClient([submit_message(LOW_RISK_REPORT)])

    result = run_agent(low_risk_input, client=client, model="fake", kb=kb, max_iterations=6)

    assert result.report.deployment_risk in {"LOW", "MEDIUM"}
    assert result.report.bug_probability == pytest.approx(0.07)
    assert result.retrieval_call_count <= 1
    assert not result.forced_finalization
    assert result.iterations_used == 1


@live
@requires_key
def test_low_risk_pr_live(low_risk_input, kb):
    result = run_agent(low_risk_input, client=build_client(), kb=kb, verbose=True)

    assert result.report.deployment_risk in {"LOW", "MEDIUM"}
    assert result.retrieval_call_count <= 1


# --------------------------------------------------------------------------- #
# 3. Forced finalization when the iteration budget is exhausted
# --------------------------------------------------------------------------- #


def test_forced_finalization_produces_valid_report(high_risk_input, kb):
    """With a budget of 1, the agent burns it on retrieval and must be forced to conclude."""

    def assert_only_submit_tool(kwargs):
        names = [t["function"]["name"] for t in kwargs["tools"]]
        assert names == [SUBMIT_TOOL_NAME], "retrieval must be withdrawn when forcing"
        assert kwargs["tool_choice"]["function"]["name"] == SUBMIT_TOOL_NAME
        return submit_message(HIGH_RISK_REPORT)

    client = FakeKimiClient(
        [
            retrieve_message("SQL injection prevention", "owasp_security", "c1"),
            assert_only_submit_tool,
        ]
    )

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb, max_iterations=1)

    assert isinstance(result.report, DeploymentReport)
    assert result.forced_finalization
    assert result.report.deployment_risk == "HIGH"
    assert result.retrieval_call_count == 1


def test_forced_finalization_with_zero_budget(high_risk_input, kb):
    """max_iterations=0 skips the main loop entirely and goes straight to forcing."""
    client = FakeKimiClient([submit_message(HIGH_RISK_REPORT)])

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb, max_iterations=0)

    assert isinstance(result.report, DeploymentReport)
    assert result.forced_finalization
    assert result.retrieval_call_count == 0


def test_forced_finalization_raises_rather_than_returning_none(high_risk_input, kb):
    """If the model never submits, the caller gets an error, not a silent None."""
    client = FakeKimiClient(
        [FakeMessage(content="I cannot decide."), FakeMessage(content="Still cannot.")]
    )

    with pytest.raises(RuntimeError, match="valid DeploymentReport"):
        run_agent(high_risk_input, client=client, model="fake", kb=kb, max_iterations=0)


@live
@requires_key
def test_forced_finalization_live(high_risk_input, kb):
    result = run_agent(
        high_risk_input, client=build_client(), kb=kb, max_iterations=1, verbose=True
    )

    assert isinstance(result.report, DeploymentReport)
    assert result.report.deployment_risk in {"LOW", "MEDIUM", "HIGH"}


# --------------------------------------------------------------------------- #
# Loop robustness
# --------------------------------------------------------------------------- #


def test_invalid_report_is_returned_for_correction(high_risk_input, kb):
    """A schema-invalid submission is fed back as an error, not raised at the caller."""
    bad = dict(HIGH_RISK_REPORT, deployment_risk="CATASTROPHIC")
    client = FakeKimiClient([submit_message(bad), submit_message(HIGH_RISK_REPORT)])

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb, max_iterations=4)

    assert result.report.deployment_risk == "HIGH"
    assert result.iterations_used == 2


def test_bug_probability_is_corrected_to_ml_value(high_risk_input, kb):
    """The passthrough field is reconciled against the ML result rather than trusted."""
    drifted = dict(HIGH_RISK_REPORT, bug_probability=0.8)
    client = FakeKimiClient([submit_message(drifted)])

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb)

    assert result.report.bug_probability == pytest.approx(0.84)


def test_empty_sources_consulted_is_filled_from_actual_retrievals(high_risk_input, kb):
    stripped = dict(HIGH_RISK_REPORT, sources_consulted=[])
    client = FakeKimiClient(
        [
            retrieve_message("SQL injection prevention", "owasp_security", "c1"),
            submit_message(stripped),
        ]
    )

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb)

    assert result.report.sources_consulted == ["owasp_security"]


def test_plain_text_tool_call_is_recovered(high_risk_input, kb):
    """The Kimi fallback: a tool call emitted as text instead of a tool_calls entry."""
    as_text = json.dumps({"name": SUBMIT_TOOL_NAME, "arguments": HIGH_RISK_REPORT})
    client = FakeKimiClient([FakeMessage(content=f"```json\n{as_text}\n```")])

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb)

    assert result.report.deployment_risk == "HIGH"
    assert result.tool_calls[-1].recovered_from_text


def test_plain_text_without_a_tool_call_is_nudged(high_risk_input, kb):
    """Unrecoverable plain text costs an iteration and prompts a retry, not a crash."""
    client = FakeKimiClient(
        [FakeMessage(content="Let me think about this."), submit_message(HIGH_RISK_REPORT)]
    )

    result = run_agent(high_risk_input, client=client, model="fake", kb=kb, max_iterations=3)

    assert result.report.deployment_risk == "HIGH"
    assert result.iterations_used == 2


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "No tool call here, just prose about deployment risk.",
        '{"unrelated": "json"}',
    ],
)
def test_fallback_parser_returns_none_on_non_tool_text(content):
    assert try_parse_tool_call_from_text(content) is None


def test_fallback_parser_recovers_bare_argument_objects():
    call = try_parse_tool_call_from_text('{"query": "SQL injection", "domain": "owasp_security"}')
    assert call is not None
    assert call.function.name == RETRIEVE_TOOL_NAME
    assert json.loads(call.function.arguments)["domain"] == "owasp_security"


# --------------------------------------------------------------------------- #
# Retrieval layer
# --------------------------------------------------------------------------- #


def test_retrieval_respects_domain_filter(kb):
    result = kb.retrieve("parameterized queries prevent injection", domain="owasp_security")
    assert result["results"], "expected at least one hit"
    assert {r["domain"] for r in result["results"]} == {"owasp_security"}


def test_retrieval_across_all_domains(kb):
    result = kb.retrieve("zero downtime schema change backfill", domain="any")
    assert result["results"]
    assert result["results"][0]["domain"] == "db_migration"


def test_retrieval_rejects_unknown_domain(kb):
    result = kb.retrieve("anything", domain="not_a_domain")
    assert result["results"] == []
    assert "error" in result


def test_submit_tool_schema_mirrors_the_model():
    from tools import SUBMIT_TOOL

    props = SUBMIT_TOOL["function"]["parameters"]["properties"]
    assert set(props) == set(DeploymentReport.model_fields)
    assert props["deployment_risk"]["enum"] == ["LOW", "MEDIUM", "HIGH"]
