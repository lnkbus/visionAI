"""한국어 토크나이저 — 희소 검색(BM25)의 입력.

형태소 분석기(kiwi/mecab)를 쓰면 정확하지만 폐쇄망 반입 대상이 하나 늘고
설치가 무거워진다. 1차 구현은 **어절 + 음절 바이그램** 조합을 쓴다:

* 어절 토큰이 "결제일", "연기" 같은 정확한 키워드 매칭을 담당하고,
* 바이그램이 "결제일이/결제일을" 같은 조사 변형을 흡수한다.

약관 검색의 핵심 신호(상품명·조항 번호·전문 용어)는 대부분 명사라
이 조합으로 상당 부분 잡힌다. 형태소 분석기는 :func:`tokenize`를 대체하는
어댑터로 나중에 교체한다(docs/06 §5의 평가셋으로 이득을 측정한 뒤).
"""

from __future__ import annotations

import re

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


def tokenize(text: str) -> list[str]:
    """검색용 토큰 목록. 색인과 질의에 **같은 함수**를 써야 한다."""
    tokens: list[str] = []
    for match in _WORD.finditer(text.lower()):
        word = match.group()
        if len(word) >= MIN_TOKEN_LEN and word not in _STOPWORDS:
            tokens.append(word)
        if _HANGUL.search(word) and len(word) > MIN_TOKEN_LEN:
            # 조사가 붙은 어절에서도 어간이 겹치도록 바이그램을 함께 넣는다.
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens
