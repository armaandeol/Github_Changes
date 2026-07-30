"""Pydantic data contracts for the Agentic RAG Engine.

The two inputs (MLResult, CodeReviewFinding) are produced by upstream pipelines that
are out of scope here — we receive them as JSON. The single output (DeploymentReport)
is produced by the agent via a terminal tool call.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class FeatureVector(BaseModel):
    """The JIT change-level features fed to the XGBoost defect model (Kamei et al.).

    Extra keys are tolerated so an upstream pipeline can add features without breaking
    the contract here.
    """

    model_config = ConfigDict(extra="allow")

    ns: int = Field(description="Number of modified subsystems.")
    nd: int = Field(description="Number of modified directories.")
    nf: int = Field(description="Number of modified files.")
    ent: float = Field(description="Entropy: how the change is distributed across files.")
    la: int = Field(description="Lines added.")
    ld: int = Field(description="Lines deleted.")
    ndev: int = Field(description="Number of developers who previously touched the files.")
    age: float = Field(description="Average time interval since the files' last change.")
    nuc: float = Field(description="Number of unique prior changes to the touched files.")
    aexp: int = Field(description="Author experience (prior commits).")
    arexp: int = Field(description="Author recent experience.")
    asexp: int = Field(description="Author subsystem experience.")


class SHAPContribution(BaseModel):
    """One feature's contribution to the XGBoost output, as reported by SHAP."""

    feature_name: str
    raw_value: float = Field(description="The feature's actual value for this PR.")
    shap_value: float = Field(
        description="Signed SHAP value in log-odds space; positive increases bug risk."
    )
    direction: Literal["increases_risk", "decreases_risk"]
    rank: int = Field(description="1-based importance rank among the reported contributions.")


class SHAPExplanation(BaseModel):
    """The SHAP artifact explaining a single XGBoost prediction."""

    base_value: float = Field(
        description="Model expected value (log-odds) before any feature contribution."
    )
    output_value: float = Field(
        ge=0.0,
        le=1.0,
        description="Final model output — the bug probability in [0, 1].",
    )
    contributions: list[SHAPContribution] = Field(
        default_factory=list,
        description="Top-ranked feature contributions; not necessarily every feature.",
    )


class MLResult(BaseModel):
    """Output of the prediction pipeline: a JIT feature vector plus its SHAP explanation."""

    feature_vector: FeatureVector
    shap_explanation: SHAPExplanation

    @property
    def bug_probability(self) -> float:
        """The XGBoost output, exposed as the probability carried through to the report."""
        return self.shap_explanation.output_value


class CodeReviewFinding(BaseModel):
    """Output of the per-file LLM code review pipeline (the `llm_response` block)."""

    risk_score: int = Field(description="The reviewer's integer risk score.")
    # Left as a free string rather than an enum so an upstream reviewer that emits, e.g.,
    # 'Critical' is not rejected at the boundary.
    risk_level: str = Field(description="Categorical risk level, e.g. 'Low'/'Medium'/'High'.")
    summary: str = Field(description="Prose summary of the review.")
    top_risk_factors: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class AgentInput(BaseModel):
    """The full payload handed to the agent — one fixture file maps onto this."""

    ml_result: MLResult
    code_review_finding: CodeReviewFinding


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


class DeploymentReport(BaseModel):
    """The final merged assessment. Emitted as a `submit_final_report` tool call."""

    deployment_risk: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        description="Overall deployment risk for this pull request."
    )
    bug_probability: float = Field(
        ge=0.0, le=1.0, description="Bug probability carried through from the ML result."
    )
    ml_explanation: list[str] = Field(
        default_factory=list,
        description="Plain-language readings of the SHAP feature contributions.",
    )
    code_review_findings: list[str] = Field(
        default_factory=list,
        description="The code review findings that materially affect the risk verdict.",
    )
    engineering_guidance: list[str] = Field(
        default_factory=list,
        description="Guidance grounded in the knowledge base passages actually retrieved.",
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Concrete, ordered actions to take before or during deployment.",
    )
    sources_consulted: list[str] = Field(
        default_factory=list,
        description=(
            "Knowledge base domains actually retrieved from, e.g. 'owasp_security'. "
            "Makes the verdict auditable."
        ),
    )
