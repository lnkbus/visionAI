"""BM25 희소 검색 — 사양서 §3 모듈 3의 Sparse 축.

의존성 없이 직접 구현한다. 약 60줄이고, 폐쇄망 반입 목록을 하나라도 줄이는
편이 낫기 때문이다.

밀집 검색만으로는 "제12조", "무배당OO보험" 같은 고유 토큰을 놓친다.
임베딩은 의미를 뭉뚱그리는 반면 BM25는 정확히 그 토큰이 있는 문서를 집는다 —
약관 검색에서 두 축을 함께 쓰는 이유다.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from vai_retrieval.tokenize import tokenize

K1 = 1.5
"""용어 빈도 포화 계수. 같은 단어가 10번 나온다고 10배 관련되지는 않는다."""

B = 0.75
"""문서 길이 정규화 강도. 긴 조항이 단지 길다는 이유로 유리해지지 않게 한다."""


@dataclass
class _Posting:
    doc_ids: dict[str, int] = field(default_factory=dict)
    """문서 ID → 해당 문서 내 등장 횟수."""


class BM25Index:
    """메모리 상주 역색인.

    테넌트별로 별도 인스턴스를 두어 교차 테넌트 검색이 구조적으로 불가능하게 한다.
    """

    def __init__(self) -> None:
        self._postings: dict[str, _Posting] = {}
        self._lengths: dict[str, int] = {}
        self._total_len = 0

    @property
    def size(self) -> int:
        return len(self._lengths)

    def add(self, doc_id: str, text: str) -> None:
        if doc_id in self._lengths:
            self.remove(doc_id)
        tokens = tokenize(text)
        if not tokens:
            return
        for term, count in Counter(tokens).items():
            self._postings.setdefault(term, _Posting()).doc_ids[doc_id] = count
        self._lengths[doc_id] = len(tokens)
        self._total_len += len(tokens)

    def remove(self, doc_id: str) -> None:
        length = self._lengths.pop(doc_id, None)
        if length is None:
            return
        self._total_len -= length
        emptied: list[str] = []
        for term, posting in self._postings.items():
            posting.doc_ids.pop(doc_id, None)
            if not posting.doc_ids:
                emptied.append(term)
        for term in emptied:
            del self._postings[term]

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """(문서 ID, BM25 점수) 목록을 점수 내림차순으로 돌려준다."""
        if not self._lengths:
            return []
        avg_len = self._total_len / len(self._lengths)
        scores: dict[str, float] = {}

        for term in set(tokenize(query)):
            posting = self._postings.get(term)
            if posting is None:
                continue
            # IDF: 흔한 단어일수록 기여를 낮춘다. +0.5/+1 은 음수 IDF 방지용 보정.
            doc_freq = len(posting.doc_ids)
            idf = math.log(1 + (len(self._lengths) - doc_freq + 0.5) / (doc_freq + 0.5))
            for doc_id, freq in posting.doc_ids.items():
                norm = 1 - B + B * self._lengths[doc_id] / avg_len
                weight = idf * freq * (K1 + 1) / (freq + K1 * norm)
                scores[doc_id] = scores.get(doc_id, 0.0) + weight

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return ranked[:top_k]

    def clear(self) -> None:
        self._postings.clear()
        self._lengths.clear()
        self._total_len = 0
