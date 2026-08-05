"""한국어 토크나이저 — 희소 검색(BM25)과 해싱 임베딩의 입력.

기본 구현은 **어절 + 음절 바이그램**이다:

* 어절 토큰이 "결제일", "연기" 같은 정확한 키워드 매칭을 담당하고,
* 바이그램이 "결제일이/결제일을" 같은 조사 변형을 흡수한다.

형태소 분석기(kiwi)는 어댑터로 교체할 수 있다. 다만 **기본값으로 삼지 않았다.**
골든셋으로 재 보니 한쪽만 좋아지는 것이 아니었기 때문이다:

    스위트        syllable(기본)              kiwi
    retrieval     MRR 0.948 · nDCG 0.962      MRR 0.976 · nDCG 0.981   ↑
    grounding     정확도 1.000                정확도 0.950             ↓
    intent        정확도 0.900                정확도 0.850             ↓

검색은 분명히 좋아진다. 대신 **추천 답변 근거 검증과 음성봇 의도 분류가
나빠진다** — 그 둘의 임계값(어휘 일치 0.35, 유사도 0.20/여유 0.09)이 어절+
바이그램의 토큰 분포에 맞춰 잡힌 값이라, 토큰이 달라지면 같은 숫자가 다른
뜻이 된다. 이 결합은 docs/06의 알려진 제약에 이미 적혀 있었고, 측정이 그
예측을 그대로 확인했다.

그래서 교체는 "토크나이저를 바꾼다"가 아니라 **"세 곳의 임계값을 함께 다시
잡는다"**는 작업이다. 형태소 모델이 폐쇄망 반입 대상을 하나 늘린다는 비용도
그 결정에 포함된다.

실제 고객사 코퍼스와 상담 로그로 다시 재고 결정할 자리다. 그때 쓸 수 있도록
교체 경로와 측정 도구(``evalctl run --tokenizer kiwi``)를 열어 둔다.

**색인과 질의가 같은 토크나이저를 써야 한다.** 다르면 BM25는 물론 해싱
임베딩까지 어긋나는데, 오류가 아니라 "검색이 좀 이상하다"로만 나타난다.
그래서 프로세스 전역에 하나만 두고(:func:`set_tokenizer`), 블록은 기동 시
그 이름을 로그로 남긴다.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_WORD = re.compile(r"[0-9A-Za-z가-힣]+")
_HANGUL = re.compile(r"[가-힣]")

# 조사·어미는 문서 간 변별력이 없어 색인에서 뺀다.
_STOPWORDS = frozenset(
    """
    은 는 이 가 을 를 의 에 에서 으로 로 와 과 도 만 까지 부터 에게 한테
    그리고 그러나 또는 및 등 수 것 때 통해 대한 위한 있는 없는 하는 합니다 입니다
    """.split()
)

MIN_TOKEN_LEN = 2

CONTENT_TAGS = frozenset(
    {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "VX", "MAG", "MAJ", "XR", "XPN", "SL", "SN", "SH"}
)
"""형태소 분석에서 남길 품사. 조사·어미·기호는 문서 간 변별력이 없다."""


class BaseTokenizer(ABC):
    name: str

    @abstractmethod
    def tokenize(self, text: str) -> list[str]:
        """검색용 토큰 목록."""


class SyllableTokenizer(BaseTokenizer):
    """어절 + 음절 바이그램. 사전도 모델도 필요 없다."""

    name = "syllable"

    def tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        for match in _WORD.finditer(text.lower()):
            word = match.group()
            if len(word) >= MIN_TOKEN_LEN and word not in _STOPWORDS:
                tokens.append(word)
            if _HANGUL.search(word) and len(word) > MIN_TOKEN_LEN:
                # 조사가 붙은 어절에서도 어간이 겹치도록 바이그램을 함께 넣는다.
                tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
        return tokens


class KiwiTokenizer(BaseTokenizer):
    """형태소 분석기(kiwipiepy). 폐쇄망 번들에 모델을 함께 반입해야 한다.

    형태소만 쓰면 "재발급"이 접두사가 떨어져 "발급"과 같아진다 — 약관에서는
    다른 조항이다. 그래서 **원래 어절도 함께 넣는다**: 형태소가 활용형을
    흡수하고, 어절이 정확한 용어를 지킨다.
    """

    name = "kiwi"

    def __init__(self) -> None:
        self._kiwi: Any = None

    def _engine(self) -> Any:
        if self._kiwi is None:
            from kiwipiepy import Kiwi

            self._kiwi = Kiwi()
        return self._kiwi

    def tokenize(self, text: str) -> list[str]:
        tokens = [
            token.form.lower()
            for token in self._engine().tokenize(text)
            if token.tag in CONTENT_TAGS
        ]
        tokens.extend(
            match.group()
            for match in _WORD.finditer(text.lower())
            if len(match.group()) >= MIN_TOKEN_LEN and match.group() not in _STOPWORDS
        )
        return tokens


TOKENIZERS: dict[str, Callable[[], BaseTokenizer]] = {
    "syllable": SyllableTokenizer,
    "kiwi": KiwiTokenizer,
}


def create_tokenizer(name: str) -> BaseTokenizer:
    factory = TOKENIZERS.get(name)
    if factory is None:
        raise ValueError(f"알 수 없는 토크나이저: {name} (가능: {', '.join(sorted(TOKENIZERS))})")
    return factory()


_active: BaseTokenizer = SyllableTokenizer()


def set_tokenizer(tokenizer: BaseTokenizer | str) -> BaseTokenizer:
    """프로세스 전역 토크나이저를 바꾼다.

    전역인 것이 의도다. 색인 경로와 질의 경로가 서로 다른 토크나이저를 쓰면
    검색이 조용히 어긋나므로, 한 프로세스 안에서는 갈릴 수 없게 만든다.
    **프로세스가 다르면(RAG-KB와 RAG-SRCH) 설정을 같게 맞추는 것은 배포의 책임**이고,
    그래서 각 블록이 기동 시 이 이름을 로그로 남긴다.
    """
    global _active
    _active = create_tokenizer(tokenizer) if isinstance(tokenizer, str) else tokenizer
    return _active


def active_tokenizer() -> BaseTokenizer:
    return _active


def tokenize(text: str) -> list[str]:
    """검색용 토큰 목록. 색인과 질의에 **같은 함수**를 써야 한다."""
    return _active.tokenize(text)
