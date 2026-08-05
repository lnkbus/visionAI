"""읽기 사전이 실제로 소리를 바꾸는가.

전처리 규칙은 숫자·날짜·단위처럼 규칙으로 정해지는 것만 다룬다. 고유명사와
사내 용어는 규칙이 없고 그 회사만 아는데, 그걸 코드로 받으면 안내 문구 하나에
공급사 배포가 필요해진다.

여기서 보는 것은 세 가지다: 사전이 규칙보다 **먼저** 적용되는가, 긴 표기가
이기는가, 띄어쓰기가 흡수되는가.
"""

from __future__ import annotations

import pytest

from vai_common.config_store import CachedConfig, ConfigKind, InMemoryConfigStore
from vai_contracts.authoring import TtsLexicon, TtsReading
from vai_tts_core.normalize import apply_readings, normalize
from vai_tts_core.readings import MAX_READINGS, ReadingCache, to_mapping

TENANT = "t1"


def lexicon(*readings: TtsReading, version: int = 1) -> TtsLexicon:
    return TtsLexicon(tenant_id=TENANT, version=version, readings=list(readings))


# ── 치환 ─────────────────────────────────────────────────────────────────────


def test_등록한_표기가_읽기로_바뀐다() -> None:
    assert apply_readings("무배당행복플러스 안내", {"무배당행복플러스": "무배당 행복 플러스"}) == (
        "무배당 행복 플러스 안내"
    )


def test_띄어쓰기가_다른_표기도_잡는다() -> None:
    """문서마다 붙여 쓰고 띄어 쓰는 것이 섞인다. 따로 등록하게 하면
    사전이 두 배로 늘고 한쪽은 반드시 빠뜨린다."""
    readings = {"무배당행복플러스": "무배당 행복 플러스"}

    assert apply_readings("무배당 행복 플러스 보험", readings).startswith("무배당 행복 플러스")
    assert apply_readings("무배당행복플러스 보험", readings).startswith("무배당 행복 플러스")


def test_긴_표기가_이긴다() -> None:
    """짧은 것이 먼저 걸리면 긴 표기를 등록한 의미가 사라진다."""
    readings = {"행복플러스": "해피 플러스", "무배당행복플러스": "무배당 행복 플러스"}

    assert apply_readings("무배당행복플러스", readings) == "무배당 행복 플러스"


def test_사전이_규칙보다_먼저_돈다() -> None:
    """나중에 하면 숫자·단위 규칙이 바꿔 놓은 자리를 사전이 다시 건드려
    결과가 예측 불가능해진다."""
    result = normalize("A1234 접수번호는 3개입니다", readings={"A1234": "에이 천이백삼십사"})

    assert "에이 천이백삼십사" in result
    assert "세 개" in result, "사전을 적용해도 나머지 규칙은 그대로 돌아야 한다"


def test_사전이_없으면_규칙만_돈다() -> None:
    assert normalize("50,000원") == normalize("50,000원", readings={})


def test_빈_표기는_무시한다() -> None:
    assert apply_readings("문장", {"": "무엇"}) == "문장"


# ── 사전 → 치환표 ────────────────────────────────────────────────────────────


def test_꺼진_항목은_적용하지_않는다() -> None:
    mapping = to_mapping(
        lexicon(
            TtsReading(surface="가", reading="갸"),
            TtsReading(surface="나", reading="냐", enabled=False),
        )
    )

    assert mapping == {"가": "갸"}


def test_한쪽이_빈_항목은_버린다() -> None:
    """표기만 있고 읽기가 없으면 그 말은 사라진다 — 저장은 되지만 적용은 안 된다."""
    mapping = to_mapping(lexicon(TtsReading(surface="가", reading="   ")))

    assert mapping == {}


def test_상한을_넘으면_잘리되_경고가_남는다(caplog: pytest.LogCaptureFixture) -> None:
    """조용히 자르면 운영자는 등록했는데 왜 안 읽히는지 알 수 없다."""
    entries = [
        TtsReading(surface=f"용어{index}", reading=f"읽기{index}")
        for index in range(MAX_READINGS + 10)
    ]

    with caplog.at_level("WARNING"):
        mapping = to_mapping(lexicon(*entries))

    assert len(mapping) == MAX_READINGS
    assert any("상한" in record.message for record in caplog.records)


# ── 캐시 ─────────────────────────────────────────────────────────────────────


async def test_배포된_사전을_읽는다() -> None:
    store = InMemoryConfigStore()
    await store.publish(
        ConfigKind.TTS_LEXICON, TENANT, lexicon(TtsReading(surface="가", reading="갸"))
    )
    cache = ReadingCache(CachedConfig(store, ConfigKind.TTS_LEXICON, TtsLexicon, ttl_s=0.0))

    assert await cache.readings(TENANT) == {"가": "갸"}


async def test_배포된_사전이_없으면_빈_표다() -> None:
    cache = ReadingCache(
        CachedConfig(InMemoryConfigStore(), ConfigKind.TTS_LEXICON, TtsLexicon, ttl_s=0.0)
    )

    assert await cache.readings(TENANT) == {}


async def test_버전이_오르면_다시_만든다() -> None:
    """반영이 안 되면 운영자는 저장이 안 된 줄 알고 같은 일을 반복한다."""
    store = InMemoryConfigStore()
    cache = ReadingCache(CachedConfig(store, ConfigKind.TTS_LEXICON, TtsLexicon, ttl_s=0.0))
    await store.publish(
        ConfigKind.TTS_LEXICON, TENANT, lexicon(TtsReading(surface="가", reading="갸"))
    )
    assert await cache.readings(TENANT) == {"가": "갸"}

    await store.publish(
        ConfigKind.TTS_LEXICON,
        TENANT,
        lexicon(TtsReading(surface="가", reading="캬"), version=2),
    )
    cache.invalidate(TENANT)

    assert await cache.readings(TENANT) == {"가": "캬"}
