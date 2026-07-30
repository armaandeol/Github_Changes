"""Shared fixtures: path setup and a throwaway ingested knowledge base."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import ingest as ingest_module  # noqa: E402
from schemas import AgentInput  # noqa: E402
from tools import KnowledgeBase  # noqa: E402

FIXTURES = PACKAGE_ROOT / "fixtures"


@pytest.fixture(scope="session")
def chroma_path(tmp_path_factory) -> Path:
    """Ingest the KB into a temporary Chroma store, once per test session."""
    path = tmp_path_factory.mktemp("chroma_kb")
    ingest_module.ingest(kb_dir=PACKAGE_ROOT / "knowledge_base", chroma_path=path)
    return path


@pytest.fixture
def kb(chroma_path: Path) -> KnowledgeBase:
    return KnowledgeBase(chroma_path=chroma_path)


def _load(name: str) -> AgentInput:
    return AgentInput.model_validate(json.loads((FIXTURES / name).read_text()))


@pytest.fixture
def high_risk_input() -> AgentInput:
    return _load("example_pr_142.json")


@pytest.fixture
def low_risk_input() -> AgentInput:
    return _load("example_pr_low_risk.json")
