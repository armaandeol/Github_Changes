"""CLI entry point.

    python run_report.py fixtures/example_pr_142.json
    python run_report.py fixtures/example_pr_142.json --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent import MAX_ITERATIONS, run_agent
from schemas import AgentInput


def load_input(path: Path) -> AgentInput:
    return AgentInput.model_validate_json(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the agentic RAG engine over a PR evidence fixture."
    )
    parser.add_argument("fixture", type=Path, help="path to the input JSON fixture")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log each tool call and its query"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=MAX_ITERATIONS,
        help=f"tool call budget before forced finalization (default {MAX_ITERATIONS})",
    )
    args = parser.parse_args(argv)

    if not args.fixture.exists():
        print(f"error: fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    agent_input = load_input(args.fixture)

    try:
        result = run_agent(
            agent_input, max_iterations=args.max_iterations, verbose=args.verbose
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.verbose:
        print(
            f"\n── done: {result.retrieval_call_count} retrieval call(s), "
            f"{result.iterations_used} iteration(s)"
            f"{', forced finalization' if result.forced_finalization else ''} ──\n",
            file=sys.stderr,
        )

    print(json.dumps(result.report.model_dump(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
