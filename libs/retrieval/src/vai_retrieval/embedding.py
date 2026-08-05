"""임베딩 어댑터 — 사양서 §3 모듈 3의 Dense 축.

STT/TTS/sLLM과 같은 Model-Agnostic 원칙을 임베딩에도 적용한다. SaaS는 API,
폐쇄망은 로컬 모델을 쓰지만 호출부는 차이를 모른다.

기본값 :class:`HashingEmbedder`는 모델 없이 도는 **어휘 기반 대체 구현**이다.
CI와 GPU 없는 개발 환경에서 파이프라인 전체를 돌리기 위한 것이며, 의미
유사도가 필요한 실제 품질은 로컬 임베딩 모델로만 나온다. 이름을 fake가 아니라
hashing으로 둔 이유는 실제로 동작하는 기법(feature hashing)이고, 소형 PoC에서는
BM25와 함께 쓰면 쓸 만한 하한선을 주기 때문이다.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import numpy as np

from vai_retrieval.tokenize import tokenize


class BaseEmbedder(ABC):
    name: str
    dimension: int

    @abstractmethod
    async def initialize(self, model_path: str, config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> np.ndarray:
        """``(len(texts), dimension)`` 형태의 L2 정규화된 float32 배열."""

    async def embed_query(self, text: str) -> np.ndarray:
        """질의 임베딩. 비대칭 모델(질의/문서 프리픽스가 다른 경우)은 오버라이드한다."""
        vector: np.ndarray = (await self.embed([text]))[0]
        return vector

    async def close(self) -> None:
        return None


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """행별 L2 정규화. 정규화해 두면 코사인 유사도가 내적 한 번으로 끝난다."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized: np.ndarray = (matrix / norms).astype(np.float32)
    return normalized


class HashingEmbedder(BaseEmbedder):
    """해싱 트릭 기반 어휘 임베딩. 모델·GPU·네트워크가 필요 없다."""

    name = "hashing"

    def __init__(self, dimension: int = 512) -> None:
        self.dimension = dimension

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        self.dimension = int(config.get("dimension", self.dimension))

    async def embed(self, texts: list[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in tokenize(text):
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimension
                # 부호를 함께 해싱해 서로 다른 토큰의 충돌이 상쇄되게 한다.
                sign = 1.0 if digest[4] & 1 else -1.0
                matrix[row, index] += sign
        return l2_normalize(matrix)


class SentenceTransformerEmbedder(BaseEmbedder):
    """로컬 임베딩 모델. 폐쇄망 번들에 가중치를 함께 반입한다."""

    name = "sentence_transformers"

    def __init__(self) -> None:
        self.dimension = 0
        self._model: Any = None
        self._query_prefix = ""
        self._doc_prefix = ""

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        import asyncio

        from sentence_transformers import SentenceTransformer

        self._model = await asyncio.to_thread(
            SentenceTransformer, model_path, device=config.get("device", "cpu")
        )
        self.dimension = int(self._model.get_sentence_embedding_dimension())
        # E5·BGE 계열은 질의와 문서에 서로 다른 프리픽스를 요구한다.
        # 이걸 빠뜨리면 점수가 조용히 나빠지기만 해서 원인을 찾기 어렵다.
        self._query_prefix = config.get("query_prefix", "")
        self._doc_prefix = config.get("doc_prefix", "")

    async def embed(self, texts: list[str]) -> np.ndarray:
        import asyncio

        prefixed = [f"{self._doc_prefix}{text}" for text in texts]
        vectors = await asyncio.to_thread(
            self._model.encode, prefixed, normalize_embeddings=True, show_progress_bar=False
        )
        encoded: np.ndarray = np.asarray(vectors, dtype=np.float32)
        return encoded

    async def embed_query(self, text: str) -> np.ndarray:
        import asyncio

        vector = await asyncio.to_thread(
            self._model.encode,
            [f"{self._query_prefix}{text}"],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        encoded: np.ndarray = np.asarray(vector, dtype=np.float32)[0]
        return encoded

    async def close(self) -> None:
        self._model = None


def _load_sentence_transformers() -> BaseEmbedder:
    return SentenceTransformerEmbedder()


_REGISTRY: dict[str, Callable[[], BaseEmbedder]] = {
    "hashing": HashingEmbedder,
    "sentence_transformers": _load_sentence_transformers,
}


def create_embedder(name: str) -> BaseEmbedder:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 임베딩 어댑터 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None
