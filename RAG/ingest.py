"""Build the Chroma knowledge-base collection from the markdown seed corpus.

Usage:
    python ingest.py            # rebuild ./chroma_kb from ./knowledge_base
    python ingest.py --stats    # show what is currently indexed
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import chromadb

HERE = Path(__file__).parent
KB_DIR = HERE / "knowledge_base"
CHROMA_PATH = HERE / "chroma_kb"
COLLECTION_NAME = "engineering_knowledge"

# Chunks shorter than this are headings or stray lines, not retrievable content.
MIN_CHUNK_CHARS = 120


def get_client(path: Path = CHROMA_PATH) -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=str(path))


def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split a KB document into (section_title, chunk_text) pairs.

    Paragraph-level chunking, with the enclosing `##` heading prepended to each
    chunk so a retrieved passage carries its own context.
    """
    chunks: list[tuple[str, str]] = []
    current_section = "Overview"

    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue

        heading = re.match(r"^#{1,3}\s+(.*)$", block.split("\n")[0])
        if heading and len(block.split("\n")) == 1:
            # A standalone heading line: it retitles the section, nothing to embed.
            if block.startswith("## ") or block.startswith("### "):
                current_section = heading.group(1).strip()
            continue

        if len(block) < MIN_CHUNK_CHARS:
            continue

        chunks.append((current_section, f"{current_section}\n\n{block}"))

    return chunks


def ingest(kb_dir: Path = KB_DIR, chroma_path: Path = CHROMA_PATH) -> int:
    """(Re)build the collection. Returns the number of chunks indexed."""
    client = get_client(chroma_path)

    # Rebuild from scratch so re-running ingest is idempotent rather than additive.
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )

    docs: list[str] = []
    metadatas: list[dict[str, str]] = []
    ids: list[str] = []

    for md_file in sorted(kb_dir.glob("*.md")):
        domain = md_file.stem
        pieces = chunk_markdown(md_file.read_text(encoding="utf-8"))
        for i, (section, chunk) in enumerate(pieces):
            docs.append(chunk)
            metadatas.append({"domain": domain, "section": section})
            ids.append(f"{domain}::{i}")
        print(f"  {domain}: {len(pieces)} chunks")

    if not docs:
        raise RuntimeError(f"No knowledge base documents found in {kb_dir}")

    collection.add(documents=docs, metadatas=metadatas, ids=ids)
    return len(docs)


def stats(chroma_path: Path = CHROMA_PATH) -> None:
    collection = get_client(chroma_path).get_collection(COLLECTION_NAME)
    got = collection.get(include=["metadatas"])
    by_domain: dict[str, int] = {}
    for meta in got["metadatas"]:
        by_domain[meta["domain"]] = by_domain.get(meta["domain"], 0) + 1
    print(f"Collection '{COLLECTION_NAME}': {collection.count()} chunks")
    for domain, count in sorted(by_domain.items()):
        print(f"  {domain}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the engineering knowledge base.")
    parser.add_argument("--stats", action="store_true", help="show current index stats")
    args = parser.parse_args()

    if args.stats:
        stats()
        return

    print(f"Ingesting {KB_DIR} -> {CHROMA_PATH}")
    total = ingest()
    print(f"Indexed {total} chunks into '{COLLECTION_NAME}'.")


if __name__ == "__main__":
    main()
