"""리랭커 어댑터 — 사양서 §3 모듈 3의 3단계(BGE-Reranker-v2, <80ms).

리랭킹이 필요한 이유: 밀집·희소 검색은 질의와 문서를 **따로** 인코딩해 비교하므로
"결제일 연기"와 "결제일 변경 불가"를 잘 구분하지 못한다. 크로스 인코더는 둘을
함께 보고 점수를 매겨 상위권 순서를 바로잡는다. 다만 비싸므로 상위 후보에만 건다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from vai_retrieval.tokenize import tokenize


class BaseReranker(ABC):
    name: str

    accepts_expanded_query: bool = False
    """확장된 질의를 받아도 되는가.

    질의 확장(구어→약관어)은 회수 단계를 위한 것이다. 어휘 기반 리랭커는
    그 확장어가 그대로 이득이 되지만, 크로스 인코더는 사람이 쓴 자연스러운
    문장으로 학습됐다 — "카드 잃어버렸어요 분실 신고"처럼 덧붙인 문자열을
    주면 판단이 흐려진다. 그래서 기본값은 보수적으로 ``False``이고,
    이득이 확인된 구현만 켠다.
    """

    @abstractmethod
    async def initialize(self, model_path: str, config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def score(self, query: str, passages: list[str]) -> list[float]:
        """질의-문단 쌍별 관련도. 높을수록 관련."""

    async def close(self) -> None:
        return None


class LexicalOverlapReranker(BaseReranker):
    """모델 없이 도는 기본 리랭커.

    질의 토큰이 문단에 얼마나 덮이는지(recall)를 본다. 크로스 인코더를 대신하지는
    못하지만, 하이브리드 융합 점수만 쓰는 것보다는 상위권 정렬이 안정적이다.
    GPU 프로파일이 붙으면 BGE로 교체한다.
    """

    name = "lexical"

    # 토큰 겹침만 보므로 확장어가 그대로 신호가 된다. 골든셋 실측으로
    # MRR 0.897 → 0.936 (34건).
    accepts_expanded_query = True

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        return None

    async def score(self, query: str, passages: list[str]) -> list[float]:
        query_tokens = set(tokenize(query))
        if not query_tokens:
            return [0.0] * len(passages)
        scores = []
        for passage in passages:
            covered = query_tokens & set(tokenize(passage))
            scores.append(len(covered) / len(query_tokens))
        return scores


class BgeReranker(BaseReranker):
    """사양서 지정 크로스 인코더. 폐쇄망 번들에 가중치를 함께 반입한다."""

    name = "bge"

    def __init__(self) -> None:
        self._model: Any = None

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        import asyncio

        from FlagEmbedding import FlagReranker

        self._model = await asyncio.to_thread(
            FlagReranker, model_path, use_fp16=bool(config.get("use_fp16", True))
        )

    async def score(self, query: str, passages: list[str]) -> list[float]:
        import asyncio

        if not passages:
            return []
        raw = await asyncio.to_thread(
            self._model.compute_score, [[query, passage] for passage in passages]
        )
        values = raw if isinstance(raw, list) else [raw]
        return [float(value) for value in values]

    async def close(self) -> None:
        self._model = None


def _load_bge() -> BaseReranker:
    return BgeReranker()


_REGISTRY: dict[str, Callable[[], BaseReranker]] = {
    "lexical": LexicalOverlapReranker,
    "bge": _load_bge,
}


def create_reranker(name: str) -> BaseReranker:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"알 수 없는 리랭커 '{name}'. 사용 가능: {sorted(_REGISTRY)}") from None
