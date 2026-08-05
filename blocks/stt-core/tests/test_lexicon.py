"""커스텀 사전 교정 테스트.

핵심 요구: 등록한 표기가 **실제 인식 결과에 반영**되어야 한다. 등록만 되고
인식이 안 바뀌면 운영자는 도구를 신뢰하지 않게 된다.
"""

from __future__ import annotations

import pytest

from vai_common.config_store import CachedConfig, ConfigKind, InMemoryConfigStore
from vai_contracts.authoring import Lexicon, LexiconEntry
from vai_stt_core.lexicon import LexiconCache, LexiconCorrector, measure_correction

INSURANCE = LexiconEntry(
    surface="무배당 행복플러스보험",
    variants=["무배담 행복플러스", "무배당 행복 플러스 보험"],
    category="product",
)


def lexicon(*entries: LexiconEntry, version: int = 1) -> Lexicon:
    return Lexicon(tenant_id="t1", version=version, entries=list(entries))


def test_known_misrecognition_is_corrected() -> None:
    corrector = LexiconCorrector(lexicon(INSURANCE))

    assert corrector.correct("고객님 무배담 행복플러스 가입하셨죠?") == (
        "고객님 무배당 행복플러스보험 가입하셨죠?"
    )


def test_spacing_variants_are_absorbed() -> None:
    """띄어쓰기는 STT가 가장 자주 틀린다. 공백만 다른 경우를 놓치면 사전이 무용해진다."""
    corrector = LexiconCorrector(lexicon(INSURANCE))

    for spoken in [
        "무배당행복플러스보험",
        "무배당 행복 플러스 보험",
        "무배당  행복플러스 보험",
    ]:
        assert corrector.correct(f"{spoken} 문의") == "무배당 행복플러스보험 문의", spoken


def test_longer_variant_wins_over_shorter() -> None:
    """짧은 표기가 먼저 걸리면 긴 표기의 일부만 치환되어 중복이 생긴다."""
    corrector = LexiconCorrector(
        lexicon(
            LexiconEntry(surface="행복플러스보험", variants=["행복플러스"]),
            LexiconEntry(surface="무배당 행복플러스보험", variants=["무배담 행복플러스"]),
        )
    )

    result = corrector.correct("무배담 행복플러스 상품")

    assert result == "무배당 행복플러스보험 상품"
    assert result.count("보험") == 1


def test_unrelated_text_is_untouched() -> None:
    corrector = LexiconCorrector(lexicon(INSURANCE))
    text = "이번 달 카드 대금 결제일 연기가 가능한가요?"

    assert corrector.correct(text) == text


def test_disabled_entries_are_ignored() -> None:
    """끈 항목이 계속 적용되면 운영자가 끌 방법이 없어진다."""
    corrector = LexiconCorrector(
        lexicon(LexiconEntry(surface="정답표기", variants=["오인식"], enabled=False))
    )

    assert corrector.correct("오인식 발생") == "오인식 발생"


def test_empty_lexicon_returns_input_unchanged() -> None:
    corrector = LexiconCorrector(lexicon())

    assert corrector.correct("아무 문장") == "아무 문장"
    assert corrector.rule_count == 0


def test_initial_prompt_biases_decoding() -> None:
    """후처리만으로는 미등록 변형을 못 잡는다. 디코딩 단계에서도 편향을 준다."""
    corrector = LexiconCorrector(
        lexicon(INSURANCE, LexiconEntry(surface="현대해상", category="company"))
    )

    assert "무배당 행복플러스보험" in corrector.initial_prompt
    assert "현대해상" in corrector.initial_prompt


def test_initial_prompt_is_capped() -> None:
    """프롬프트가 너무 길면 디코딩 품질이 오히려 떨어진다."""
    entries = [LexiconEntry(surface=f"매우긴상품명{i:03d}") for i in range(200)]

    corrector = LexiconCorrector(lexicon(*entries))

    assert len(corrector.initial_prompt) <= 200


def test_same_version_does_not_recompile() -> None:
    """발화마다 정규식을 다시 컴파일하면 지연 예산을 컴파일에 쓰게 된다."""
    corrector = LexiconCorrector(lexicon(INSURANCE))
    before = corrector.rule_count

    corrector.update(lexicon(LexiconEntry(surface="다른상품"), version=1))

    assert corrector.rule_count == before, "같은 버전이면 갱신하지 않아야 한다"


def test_new_version_recompiles() -> None:
    corrector = LexiconCorrector(lexicon(INSURANCE))

    corrector.update(lexicon(LexiconEntry(surface="새상품", variants=["세상품"]), version=2))

    assert corrector.version == 2
    assert corrector.correct("세상품 문의") == "새상품 문의"


def test_correction_stays_cheap_on_the_realtime_path() -> None:
    """STT 뒤에 붙는 동기 처리다. 여기서 밀리면 1초 예산이 무너진다."""
    entries = [
        LexiconEntry(surface=f"상품{i:03d}", variants=[f"상픔{i:03d}", f"상 품 {i:03d}"])
        for i in range(200)
    ]
    corrector = LexiconCorrector(lexicon(*entries))

    _, elapsed_ms = measure_correction(corrector, "고객님 상픔042 가입 문의 " * 10)

    assert elapsed_ms < 20.0, f"교정 지연 초과: {elapsed_ms:.2f}ms"


def test_entry_cap_is_enforced() -> None:
    entries = [LexiconEntry(surface=f"상품{i:04d}") for i in range(700)]

    corrector = LexiconCorrector(lexicon(*entries))

    assert corrector.rule_count <= 500


# ── 배포 채널 연동 ───────────────────────────────────────────────────────────


@pytest.fixture
def cache() -> tuple[LexiconCache, InMemoryConfigStore]:
    store = InMemoryConfigStore()
    return LexiconCache(CachedConfig(store, ConfigKind.LEXICON, Lexicon, ttl_s=0.0)), store


async def test_no_lexicon_means_no_corrector(cache) -> None:
    """사전을 배포하지 않은 테넌트에서 교정이 끼어들면 안 된다."""
    lexicons, _ = cache

    assert await lexicons.corrector("never-published") is None


async def test_published_lexicon_reaches_the_realtime_path(cache) -> None:
    """저작 도구가 배포한 사전이 STT 경로에 도달하는지 — 이 연결이 없으면
    등록만 되고 인식은 그대로다."""
    lexicons, store = cache
    await store.publish(ConfigKind.LEXICON, "t1", lexicon(INSURANCE))

    corrector = await lexicons.corrector("t1")

    assert corrector is not None
    assert corrector.correct("무배담 행복플러스") == "무배당 행복플러스보험"


async def test_republished_lexicon_is_picked_up(cache) -> None:
    lexicons, store = cache
    await store.publish(ConfigKind.LEXICON, "t1", lexicon(INSURANCE))
    await lexicons.corrector("t1")

    await store.publish(
        ConfigKind.LEXICON,
        "t1",
        lexicon(LexiconEntry(surface="신상품", variants=["신 상품"]), version=2),
    )
    corrector = await lexicons.corrector("t1")

    assert corrector is not None
    assert corrector.correct("신 상품 문의") == "신상품 문의"


async def test_lexicons_are_isolated_per_tenant(cache) -> None:
    lexicons, store = cache
    await store.publish(ConfigKind.LEXICON, "t1", lexicon(INSURANCE))

    assert await lexicons.corrector("t2") is None
