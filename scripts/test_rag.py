"""
Day 5-6: manual embeddings + Chroma storage + retrieval + LLM answer.
Still no FastAPI, no Postgres — just the raw pipeline, so you can see and
trust every step before it's wired into the app.

Usage (from inside the running app container):

    docker compose exec app python scripts/test_rag.py samples/Blockchain.pdf "What is a Hash Time Lock Contract?"

First run will download the embedding model (~90MB) — expect it to take
a minute or two the first time only.
"""

import sys
import time

from pathlib import Path

# Running this file directly only puts scripts/ on sys.path, not the project
# root — so `from app.core.config import settings` fails without this line.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chromadb
from google import genai
# import google.generativeai as genai
from sentence_transformers import SentenceTransformer

from app.core.config import settings
from test_ingestion import chunk_text, load_pages  # same scripts/ folder, reuse Day 3-4 logic

COLLECTION_NAME = "test_collection"


def build_chunks(pdf_path: str) -> list[dict]:
    """Returns a flat list of chunk dicts: {content, page_number, chunk_index}."""
    pages = load_pages(pdf_path)
    all_chunks = []
    for page_number, page_text in pages:
        page_chunks = chunk_text(page_text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
        for chunk_index, content in enumerate(page_chunks):
            all_chunks.append(
                {"content": content, "page_number": page_number, "chunk_index": chunk_index}
            )
    return all_chunks


def embed_and_store(chunks: list[dict], embedder: SentenceTransformer, collection) -> None:
    """
    Embeds every chunk and stores it in Chroma.
    Uses upsert (not add) so re-running this script on the same PDF doesn't
    error out on duplicate IDs — it just overwrites the same vectors.
    """
    print(f"Embedding {len(chunks)} chunks...")
    texts = [c["content"] for c in chunks]
    embeddings = embedder.encode(texts, show_progress_bar=True).tolist()

    ids = [f"p{c['page_number']}_c{c['chunk_index']}" for c in chunks]
    metadatas = [{"page_number": c["page_number"], "chunk_index": c["chunk_index"]} for c in chunks]

    collection.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    print(f"Stored {len(chunks)} chunks in Chroma collection '{COLLECTION_NAME}'.\n")


def retrieve(question: str, embedder: SentenceTransformer, collection, top_k: int) -> dict:
    """Embeds the question, returns Chroma's top_k nearest chunks."""
    query_embedding = embedder.encode([question]).tolist()
    results = collection.query(query_embeddings=query_embedding, n_results=top_k)
    return results


def generate_answer(question: str, retrieved: dict) -> str:
    """Builds a grounded prompt from retrieved chunks and calls the LLM."""
    context_blocks = []
    for doc, meta in zip(retrieved["documents"][0], retrieved["metadatas"][0]):
        context_blocks.append(f"[Page {meta['page_number']}]\n{doc}")
    context = "\n\n---\n\n".join(context_blocks)
 
    prompt = f"""Answer the question using ONLY the context below. If the context
doesn't contain the answer, say so — don't make anything up.
 
Context:
{context}
 
Question: {question}
 
Answer:"""
 
    genai_client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    response = genai_client.models.generate_content(
        model=settings.LLM_MODEL,
        contents=prompt,
    )
    return response.text


def main(pdf_path: str, question: str) -> None:
    print(f"Config -> TOP_K={settings.TOP_K}, EMBEDDING_MODEL={settings.EMBEDDING_MODEL}, LLM_MODEL={settings.LLM_MODEL}\n")

    print("Loading embedding model (first run downloads it, be patient)...")
    embedder = SentenceTransformer(settings.EMBEDDING_MODEL)

    print(f"Connecting to Chroma at {settings.CHROMA_HOST}:{settings.CHROMA_PORT}...")
    client = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
    # cosine distance, matching the design decision to use cosine similarity
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )

    chunks = build_chunks(pdf_path)
    embed_and_store(chunks, embedder, collection)

    print(f"Question: {question}\n")
    start = time.time()
    retrieved = retrieve(question, embedder, collection, settings.TOP_K)
    retrieval_time_ms = int((time.time() - start) * 1000)

    print(f"Retrieved top {settings.TOP_K} chunks in {retrieval_time_ms}ms:\n")
    for doc, meta, distance in zip(
        retrieved["documents"][0], retrieved["metadatas"][0], retrieved["distances"][0]
    ):
        similarity = 1 - distance  # Chroma returns cosine distance; similarity = 1 - distance
        preview = doc[:100].replace("\n", " ")
        print(f"  [page {meta['page_number']}, chunk {meta['chunk_index']}] similarity={similarity:.3f}")
        print(f"    {preview}...\n")

    start = time.time()
    answer = generate_answer(question, retrieved)
    llm_time_ms = int((time.time() - start) * 1000)

    print(f"--- Answer (LLM call took {llm_time_ms}ms) ---")
    print(answer)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print('Usage: python scripts/test_rag.py <path_to_pdf> "<question>"')
        sys.exit(1)

    main(sys.argv[1], sys.argv[2])