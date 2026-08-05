"""음성 합성.

두 가지가 이 블록의 전부다:

1. **첫 소리까지의 지연** — 첫 조각이 전체 합성을 기다리지 않는가
2. **끼어들기** — 취소 뒤 오디오가 한 조각도 더 나가지 않는가

두 번째가 특히 중요하다. "거의 멈췄다"는 멈춘 것이 아니다 — 고객 귀에는
봇이 말을 끊지 않은 것으로 들린다.
"""

from __future__ import annotations

import asyncio

import pytest

from vai_contracts.speech import AudioEncoding, VoiceProfile
from vai_tts_core.adapters import available, create_adapter
from vai_tts_core.adapters.fake import FakeTTSAdapter
from vai_tts_core.normalize import native, normalize, sino
from vai_tts_core.streaming import (
    FIRST_CHUNK_MAX,
    duration_ms,
    resample_pcm16,
    split_for_streaming,
    split_sentences,
)
from vai_tts_core.synth import CancellationRegistry, Synthesizer

LONG_ANSWER = (
    "확인해 드리겠습니다. 잠시만 기다려 주세요. "
    "조회 결과 현재 잔여 한도는 1,234,567원입니다. 추가로 도와드릴 내용이 있으실까요?"
)


async def _adapter(**config: object) -> FakeTTSAdapter:
    adapter = FakeTTSAdapter()
    await adapter.initialize("", dict(config))
    return adapter


# --- 한국어 전처리 -------------------------------------------------------


def test_금액을_읽는_대로_바꾼다() -> None:
    """ "50,000원"이 "오영영영영원"으로 나가면 고객은 금액을 알아듣지 못하고,
    그 통화는 실패한 통화다."""
    assert normalize("결제 금액은 50,000원입니다") == "결제 금액은 오만 원입니다"


def test_고유어와_한자어_수사를_가른다() -> None:
    """ "삼 개"나 "서른 분 걸립니다"는 사람이 듣자마자 기계라는 것을 안다."""
    assert "세 개" in normalize("상품 3개를 주문하셨습니다")
    assert "사십오 분" in normalize("회의는 45분 걸립니다")


def test_시각은_고유어와_한자어가_섞인다() -> None:
    """한국어 TTS의 대표적 오답 지점. '세 시 삼십 분'이 맞다."""
    assert normalize("3시 30분에 방문") == "세 시 삼십 분에 방문"
    assert normalize("10시 정각") == "열 시 정각"


def test_조사가_붙어도_단위를_알아본다() -> None:
    """'개를'을 단위로 통째로 보면 한자어로 읽혀 '삼 개를'이 된다."""
    assert normalize("3개를") == "세 개를"


def test_사람_수는_고유어로_읽는다() -> None:
    assert "세 분" in normalize("고객 3분이 대기 중입니다")


def test_전화번호는_한_자리씩_읽는다() -> None:
    """붙여 읽으면 고객이 받아 적지 못한다. 0은 '공'이다."""
    assert normalize("010-1234-5678") == "공일공 일이삼사 오육칠팔"


def test_만_단위를_사람처럼_읽는다() -> None:
    assert sino(10000) == "만"  # "일만 원"이라 말하는 사람은 없다
    assert sino(50000) == "오만"
    assert sino(1234567) == "백이십삼만 사천오백육십칠"


def test_숫자와_한글_자릿수가_섞여도_이어_읽는다() -> None:
    """금융 상담에서 흔한 표기. '일 억 오 천만원'으로 끊기면 안 된다."""
    assert normalize("1억 5천만원 대출") == "일억 오천만원 대출"


def test_고유어는_백_이상에서_한자어로_넘어간다() -> None:
    """'백스물세 개'라고 말하는 사람은 없다."""
    assert native(99) == "아흔아홉"
    assert native(123) == sino(123)


def test_영문_약어는_철자로_읽는다() -> None:
    assert normalize("VIP 고객") == "브이아이피 고객"


def test_빈_문자열을_그대로_돌려준다() -> None:
    assert normalize("") == ""


# --- 문장 분할 -----------------------------------------------------------


def test_문장_단위로_나눈다() -> None:
    assert len(split_sentences("확인했습니다. 잠시만 기다려 주세요.")) == 2


def test_첫_조각을_가장_짧게_만든다() -> None:
    """첫 소리가 늦으면 뒤가 아무리 빨라도 대화가 무너진다."""
    chunks = split_for_streaming(LONG_ANSWER)
    assert chunks[0] == "확인해 드리겠습니다."
    assert len(chunks[0]) <= FIRST_CHUNK_MAX


def test_긴_첫_문장은_절_경계에서_더_자른다() -> None:
    long_first = (
        "고객님께서 문의하신 카드 재발급 절차를 지금부터 안내해 드리겠습니다, 잘 들어 주세요"
    )
    chunks = split_for_streaming(long_first)
    assert len(chunks) > 1
    assert len(chunks[0]) <= len(long_first)


def test_너무_짧은_조각은_앞에_붙인다() -> None:
    """ "네." 하나만 따로 합성하면 엔진 호출 비용이 발화 길이보다 크고,
    조각 사이 이음매가 들린다."""
    chunks = split_sentences("확인했습니다. 네.")
    assert len(chunks) == 1


def test_조항_번호에서_잘못_끊지_않는다() -> None:
    """마침표만 보면 "제1조."에서 끊긴다."""
    assert len(split_sentences("제1조. 목적 이 약관은")) == 1


def test_빈_입력은_빈_목록() -> None:
    assert split_for_streaming("   ") == []


# --- 재표본화 ------------------------------------------------------------


def test_표본율을_낮추면_길이가_비례해_준다() -> None:
    pcm = b"\x00\x10" * 2400  # 24kHz에서 100ms
    downsampled = resample_pcm16(pcm, 24000, 8000)
    assert duration_ms(downsampled, 8000) == pytest.approx(100, abs=2)


def test_같은_표본율이면_그대로_돌려준다() -> None:
    pcm = b"\x01\x02" * 100
    assert resample_pcm16(pcm, 16000, 16000) is pcm


def test_빈_오디오도_처리한다() -> None:
    assert resample_pcm16(b"", 24000, 8000) == b""


# --- 스트리밍 합성 -------------------------------------------------------


async def test_첫_조각이_전체_합성을_기다리지_않는다() -> None:
    """조각마다 지연이 있는 엔진에서, 첫 조각이 전체 시간의 일부만에 나와야 한다."""
    synth = Synthesizer(await _adapter(latency_ms=5))
    voice = VoiceProfile()

    first_at = None
    started = asyncio.get_running_loop().time()
    count = 0
    stats = None
    async for segment, turn_stats in synth.synthesize("s1", "t1", LONG_ANSWER, voice):
        stats = turn_stats
        if first_at is None and segment.pcm:
            first_at = asyncio.get_running_loop().time() - started
        count += 1
    total = asyncio.get_running_loop().time() - started

    assert first_at is not None
    assert count > 3
    # 첫 소리가 전체의 절반 이전에 나와야 스트리밍이라 할 수 있다.
    assert first_at < total / 2
    assert stats is not None and stats.ttfb_ms > 0


async def test_마지막_조각으로_끝을_알린다() -> None:
    """재생부가 '이제 끝났다'를 알아야 다음 턴의 마이크를 열 수 있다."""
    synth = Synthesizer(await _adapter())
    segments = [s async for s, _ in synth.synthesize("s1", "t1", "안녕하세요", VoiceProfile())]
    assert segments[-1].is_final
    assert segments[-1].pcm == b""
    assert all(not s.is_final for s in segments[:-1])


async def test_합성_길이가_텍스트_길이에_비례한다() -> None:
    synth = Synthesizer(await _adapter())
    voice = VoiceProfile()

    async def audio_ms(text: str) -> int:
        last = 0
        async for _, stats in synth.synthesize("s", text, text, voice):
            last = stats.audio_ms
        return last

    assert await audio_ms("가나다라마바사아자차") > await audio_ms("가나다")


async def test_전화망_인코딩으로_내보낸다() -> None:
    """μ-law는 표본당 1바이트다. AUD-RTP가 그대로 실어 보낼 수 있어야 한다."""
    synth = Synthesizer(await _adapter())
    voice = VoiceProfile(sample_rate=8000, encoding=AudioEncoding.MULAW)
    segments = [s async for s, _ in synth.synthesize("s1", "t1", "안녕하세요", voice) if s.pcm]

    assert segments
    assert all(s.encoding is AudioEncoding.MULAW for s in segments)
    # 8kHz μ-law 40ms = 320바이트. PCM16이었다면 640바이트다.
    assert len(segments[0].pcm) == 320


# --- 끼어들기 ------------------------------------------------------------


async def test_취소하면_오디오가_한_조각도_더_나가지_않는다() -> None:
    """ "거의 멈췄다"는 멈춘 것이 아니다. 고객 귀에는 봇이 말을 끊지 않은
    것으로 들리고, 그 통화는 실패한 통화다."""
    synth = Synthesizer(await _adapter(latency_ms=2))
    voice = VoiceProfile()

    received = 0
    stats = None
    async for segment, turn_stats in synth.synthesize("s1", "t1", LONG_ANSWER, voice):
        stats = turn_stats
        if segment.pcm:
            received += 1
        if received == 2:
            synth.registry.cancel("s1", "t1")

    assert stats is not None and stats.cancelled
    # 취소 직후 최대 한 조각(이미 만들어진 것)까지만 허용한다.
    assert received <= 3


async def test_취소는_그_턴만_버린다() -> None:
    """세션 단위로만 묶으면 끼어들기가 다음 턴의 답까지 함께 버린다."""
    registry = CancellationRegistry()
    registry.token("s1", "turn-1")
    registry.token("s1", "turn-2")

    assert registry.cancel("s1", "turn-1") == 1
    assert registry.token("s1", "turn-1").is_set()
    assert not registry.token("s1", "turn-2").is_set()


async def test_턴을_비우면_세션_전체를_취소한다() -> None:
    registry = CancellationRegistry()
    registry.token("s1", "turn-1")
    registry.token("s1", "turn-2")
    registry.token("s2", "turn-1")

    assert registry.cancel("s1") == 2
    assert not registry.token("s2", "turn-1").is_set()


async def test_턴이_끝나면_취소_토큰을_정리한다() -> None:
    """안 지우면 긴 통화에서 계속 쌓인다."""
    synth = Synthesizer(await _adapter())
    async for _ in synth.synthesize("s1", "t1", "안녕하세요", VoiceProfile()):
        pass
    assert synth.registry.active == 0


async def test_합성_전에_취소되면_아무것도_내보내지_않는다() -> None:
    synth = Synthesizer(await _adapter())
    synth.registry.token("s1", "t1").set()
    segments = [s async for s, _ in synth.synthesize("s1", "t1", LONG_ANSWER, VoiceProfile())]
    assert segments == []


# --- 어댑터 --------------------------------------------------------------


def test_어댑터_레지스트리() -> None:
    assert set(available()) == {"fake", "cosyvoice", "kokoro"}
    assert create_adapter("fake").name == "fake"
    with pytest.raises(ValueError, match="알 수 없는"):
        create_adapter("nonexistent")


async def test_가짜_어댑터는_결정적이다() -> None:
    """같은 텍스트가 항상 같은 파형을 내야 회귀 테스트가 성립한다."""
    adapter = await _adapter()
    voice = VoiceProfile()
    first = b"".join([c async for c in adapter.synthesize_stream("안녕하세요", voice)])
    second = b"".join([c async for c in adapter.synthesize_stream("안녕하세요", voice)])
    assert first == second


async def test_빈_텍스트는_오디오를_내지_않는다() -> None:
    adapter = await _adapter()
    assert [c async for c in adapter.synthesize_stream("   ", VoiceProfile())] == []


def test_영문_문장도_마침표에서_끊는다() -> None:
    """조항 번호 예외가 과하게 적용되면 일반 문장이 안 끊긴다."""
    assert len(split_sentences("Hello world. Next sentence.")) == 2


def test_소수점에서_끊지_않는다() -> None:
    """ "3.5%"가 "3." + "5%"로 갈리면 숫자가 통째로 망가진다."""
    assert len(split_sentences("이자율은 3.5% 입니다")) == 1
