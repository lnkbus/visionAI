"""RAG-KB — 지식베이스 수집기 블록."""

from vai_rag_kb.app import BLOCK_ID, create_app
from vai_rag_kb.indexer import DocumentStore, IngestionPipeline, build_chunks, new_doc_id

__all__ = [
    "BLOCK_ID",
    "DocumentStore",
    "IngestionPipeline",
    "build_chunks",
    "create_app",
    "new_doc_id",
]
