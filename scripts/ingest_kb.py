"""
One-off/cron script: chunk a plain-text knowledge base source file and ingest it into
divine_chatbot_kb_documents / divine_chatbot_kb_chunks via Gemini embeddings.

Usage:
  python scripts/ingest_kb.py --title "Project X Pricing" --category pricing path/to/source.txt

Chunking is a simple fixed-size paragraph splitter - good enough for pricing sheets,
RERA docs, and project spec sheets. Re-run is safe to do per-document (each run creates
a new document_id + fresh chunks; delete the old document's chunks manually if replacing).
"""
import sys
import uuid
import argparse

from Divinepersistence import persistenceChatbot
from DivineService.llm_gemini import llmGemini

CHUNK_MAX_CHARS = 1200


def chunk_text(raw: str) -> list:
    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > CHUNK_MAX_CHARS and current:
            chunks.append(current.strip())
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_file")
    parser.add_argument("--title", required=True)
    parser.add_argument("--category", required=True, help="pricing | rera | project_specs | location | loan")
    args = parser.parse_args()

    with open(args.source_file, "r", encoding="utf-8") as f:
        raw = f.read()

    chunks = chunk_text(raw)
    if not chunks:
        print("No content to ingest.")
        sys.exit(1)

    persistence = persistenceChatbot()
    gemini = llmGemini()

    document = persistence.create_kb_document(id=str(uuid.uuid4()), title=args.title, category=args.category, source_uri=args.source_file)
    print(f"Created document {document.id} ({len(chunks)} chunks)")

    for index, chunk in enumerate(chunks):
        embedding = gemini.embed(chunk)
        persistence.create_kb_chunk(id=str(uuid.uuid4()), document_id=document.id, chunk_index=index, content=chunk, embedding=embedding)
        print(f"  chunk {index + 1}/{len(chunks)} ingested")

    print("Done.")


if __name__ == "__main__":
    main()
