"""Tool definitions and implementations exposed to the agent.

Two tools:
  - `retrieve_engineering_knowledge` — similarity search over the Chroma KB.
  - `submit_final_report`            — the terminal action, whose parameter schema is
                                       generated from the DeploymentReport model so
                                       structured output is guaranteed to validate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ingest import COLLECTION_NAME, CHROMA_PATH, get_client
from schemas import DeploymentReport

KB_DOMAINS = [
    "owasp_security",
    "google_eng_practices",
    "ms_secure_coding",
    "deployment_best_practices",
    "feature_flags",
    "api_versioning",
    "db_migration",
]

TOP_K = 3

RETRIEVE_TOOL_NAME = "retrieve_engineering_knowledge"
SUBMIT_TOOL_NAME = "submit_final_report"


# --------------------------------------------------------------------------- #
# Tool schemas
# --------------------------------------------------------------------------- #

RETRIEVE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": RETRIEVE_TOOL_NAME,
        "description": (
            "Search the engineering knowledge base for guidance relevant to a specific "
            "concern raised by the ML prediction or code review. Call this once per "
            "distinct concern you want to investigate, not once for the whole PR."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The specific concern to search for, e.g. 'SQL injection "
                        "prevention' or 'authentication deployment rollout'"
                    ),
                },
                "domain": {
                    "type": "string",
                    "enum": [*KB_DOMAINS, "any"],
                    "description": (
                        "Restrict the search to one KB domain, or 'any' to search "
                        "across all of them"
                    ),
                },
            },
            "required": ["query", "domain"],
        },
    },
}


def _strip_titles(schema: Any) -> Any:
    """Remove pydantic's `title` keys — noise the model does not need."""
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def build_submit_tool() -> dict[str, Any]:
    """Mirror the DeploymentReport JSON schema exactly rather than hand-writing it."""
    params = _strip_titles(DeploymentReport.model_json_schema())
    params.setdefault("required", list(DeploymentReport.model_fields))
    return {
        "type": "function",
        "function": {
            "name": SUBMIT_TOOL_NAME,
            "description": (
                "Submit the final, complete deployment risk report. Call this exactly "
                "once, only after you have gathered enough evidence to justify every "
                "field."
            ),
            "parameters": params,
        },
    }


SUBMIT_TOOL: dict[str, Any] = build_submit_tool()

ALL_TOOLS: list[dict[str, Any]] = [RETRIEVE_TOOL, SUBMIT_TOOL]


# --------------------------------------------------------------------------- #
# Retrieval implementation
# --------------------------------------------------------------------------- #


class KnowledgeBase:
    """Thin wrapper over the persistent Chroma collection.

    Also records which domains were actually returned, so the orchestrator can
    cross-check the agent's self-reported `sources_consulted`.
    """

    def __init__(self, chroma_path: Path | str = CHROMA_PATH) -> None:
        self._chroma_path = Path(chroma_path)
        self._collection = None
        self.domains_retrieved: list[str] = []
        self.queries: list[dict[str, str]] = []

    @property
    def collection(self):
        if self._collection is None:
            client = get_client(self._chroma_path)
            try:
                self._collection = client.get_collection(COLLECTION_NAME)
            except Exception as exc:  # collection missing -> actionable message
                raise RuntimeError(
                    f"Knowledge base collection '{COLLECTION_NAME}' not found at "
                    f"{self._chroma_path}. Run `python ingest.py` first."
                ) from exc
        return self._collection

    def retrieve(self, query: str, domain: str = "any", top_k: int = TOP_K) -> dict[str, Any]:
        """Run the similarity search backing `retrieve_engineering_knowledge`."""
        if not query or not query.strip():
            return {"error": "query must be a non-empty string", "results": []}
        if domain != "any" and domain not in KB_DOMAINS:
            return {
                "error": f"unknown domain '{domain}'; valid: {KB_DOMAINS + ['any']}",
                "results": [],
            }

        where = None if domain == "any" else {"domain": domain}
        raw = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        results = []
        for doc, meta, dist in zip(
            raw["documents"][0], raw["metadatas"][0], raw["distances"][0]
        ):
            results.append(
                {
                    "domain": meta["domain"],
                    "section": meta.get("section", ""),
                    "relevance": round(1.0 - float(dist), 3),
                    "content": doc,
                }
            )
            if meta["domain"] not in self.domains_retrieved:
                self.domains_retrieved.append(meta["domain"])

        self.queries.append({"query": query, "domain": domain})
        return {"query": query, "domain_filter": domain, "results": results}
