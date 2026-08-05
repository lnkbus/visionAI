"""검색 인프라 — RAG-KB(색인)와 RAG-SRCH(조회)가 공유한다.

두 블록은 서로를 import 하지 않지만 같은 색인을 바라봐야 한다. 공유 대상을
블록이 아니라 명시적인 라이브러리로 두어 블록 독립성을 지킨다.
"""

from vai_retrieval.bm25 import BM25Index
from vai_retrieval.chunking import ChunkingConfig, split_text
from vai_retrieval.embedding import BaseEmbedder, HashingEmbedder, create_embedder
from vai_retrieval.hybrid import HybridSearchEngine, SearchBudget
from vai_retrieval.rerank import BaseReranker, create_reranker
from vai_retrieval.store import BaseVectorStore, MemoryVectorStore, create_store
from vai_retrieval.tokenize import tokenize

__all__ = [
    "BM25Index",
    "BaseEmbedder",
    "BaseReranker",
    "BaseVectorStore",
    "ChunkingConfig",
    "HashingEmbedder",
    "HybridSearchEngine",
    "MemoryVectorStore",
    "SearchBudget",
    "create_embedder",
    "create_reranker",
    "create_store",
    "split_text",
    "tokenize",
]
