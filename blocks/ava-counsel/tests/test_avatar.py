"""아바타 입 모양.

**동기가 전부다.** 소리와 입 모양이 어긋나면 사람은 즉시 알아채고, 어긋난
아바타는 없느니만 못하다. 그래서 타이밍이 실측 길이를 벗어나지 않는지,
그리고 양순음에서 입이 실제로 닫히는지를 고정한다.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from vai_ava_counsel.viseme import (
    MIN_FRAME_MS,
    build_track,
    decompose,
    syllable_visemes,
)
from vai_ava_counsel.worker import AvatarWorker
from vai_common.bus import InMemoryEventBus
from vai_contracts.avatar import AvatarState, AvatarTrack, Viseme
from vai_contracts.speech import SpeechChunk
from vai_contracts.topics import Topic


def _visemes(text: str, duration_ms: int = 1000) -> list[Viseme]:
    return [f.viseme for f in build_track(text, duration_ms)]


# --- 한글 분해 -----------------------------------------------------------


def test_음절을_초중종성으로_나눈다() -> None:
    assert decompose("맘") == ("ㅁ", "ㅏ", "ㅁ")
    assert decompose("안") == ("ㅇ", "ㅏ", "ㄴ")
    assert decompose("가") == ("ㄱ", "ㅏ", "")


def test_한글이_아니면_None() -> None:
    assert decompose("A") is None
    assert decompose("5") is None


# --- 입 모양 -------------------------------------------------------------


def test_양순음은_입술을_닫는다() -> None:
    """사람은 다른 입 모양의 오차는 잘 못 느끼지만 "마"에서 입이 안 닫히면
    즉시 가짜라고 안다."""
    shapes = [v for v, _ in syllable_visemes("마")]
    assert shapes[0] is Viseme.PP


def test_양순음에_더_긴_시간을_준다() -> None:
    """입술이 닫히는 순간이 짧으면 눈에 안 보이고, "마"가 "아"로 보인다."""
    bilabial = dict(syllable_visemes("마"))[Viseme.PP]
    other = dict(syllable_visemes("가"))[Viseme.KK]
    assert bilabial > other


def test_받침도_입_모양에_들어간다() -> None:
    """없으면 "밥"이 "바"처럼 보인다."""
    shapes = [v for v, _ in syllable_visemes("밥")]
    assert shapes == [Viseme.PP, Viseme.AA, Viseme.PP]


def test_초성_이응은_입_모양을_만들지_않는다() -> None:
    """소리가 없는 자음이다. 넣으면 없는 자음이 보인다."""
    assert [v for v, _ in syllable_visemes("아")] == [Viseme.AA]


def test_모음마다_다른_입_모양() -> None:
    """전부 같은 모양이면 아바타가 입만 뻐끔거리는 것처럼 보인다."""
    vowels = {syllable_visemes(s)[-1][0] for s in ("아", "이", "오", "우", "에")}
    assert len(vowels) == 5


def test_비율의_합은_1이다() -> None:
    for syllable in ("마", "가", "밥", "안", "쓰"):
        assert sum(r for _, r in syllable_visemes(syllable)) == pytest.approx(1.0)


# --- 타임라인 ------------------------------------------------------------


def test_타임라인이_실측_길이를_넘지_않는다() -> None:
    """넘으면 오디오가 끝난 뒤에도 입이 움직인다."""
    frames = build_track("안녕하세요 고객님", 1200)
    assert frames[-1].at_ms <= 1200


def test_시간_순서가_유지된다() -> None:
    frames = build_track("확인해 드리겠습니다", 900)
    times = [f.at_ms for f in frames]
    assert times == sorted(times)


def test_길이가_두_배면_시점도_두_배() -> None:
    """글자 수가 아니라 실측 길이에 맞춘다 — 추정하면 문장 끝에서 벌어진다."""
    short = build_track("안녕하세요", 500)
    long = build_track("안녕하세요", 1000)
    assert len(short) == len(long)
    assert long[len(long) // 2].at_ms == pytest.approx(short[len(short) // 2].at_ms * 2, abs=2)


def test_끝에_입을_다문다() -> None:
    """마지막 모양이 남으면 아바타가 입을 벌린 채 굳어 있는 것처럼 보인다."""
    assert build_track("안녕하세요", 800)[-1].viseme is Viseme.SIL


def test_같은_모양이_이어지면_합친다() -> None:
    """나눠 보내도 화면은 똑같고 전송량만 는다 — 통화당 수백 프레임이 쌓인다."""
    frames = build_track("아아아아", 2000)
    shapes = [f.viseme for f in frames]
    # aa가 연달아 나오지 않아야 한다(끝의 sil 제외).
    assert shapes.count(Viseme.AA) == 1


def test_너무_짧은_프레임은_만들지_않는다() -> None:
    """60fps에서도 한 프레임 남짓이라 그려지지 않고 개수만 는다."""
    frames = build_track("안녕하세요 고객님 무엇을 도와드릴까요", 300)
    gaps = [b.at_ms - a.at_ms for a, b in pairwise(frames)]
    assert all(gap >= MIN_FRAME_MS or gap == 0 for gap in gaps[:-1])


def test_길이가_0이면_빈_타임라인() -> None:
    assert build_track("안녕", 0) == []


def test_공백만_있으면_무음_한_프레임() -> None:
    frames = build_track("   ", 500)
    assert len(frames) == 1 and frames[0].viseme is Viseme.SIL


def test_한글이_아닌_글자도_처리한다() -> None:
    """전처리를 거치면 대부분 한글이 되지만, 남은 것에서 죽으면 안 된다."""
    assert _visemes("ABC 123") != []


def test_문장이_길어져도_어긋나지_않는다() -> None:
    """글자 수로 추정하면 문장이 길어질수록 누적돼 끝에서 눈에 띄게 벌어진다."""
    text = "고객님께서 문의하신 카드 재발급 절차를 지금부터 안내해 드리겠습니다"
    frames = build_track(text, 4000)
    assert frames[-1].at_ms == 4000
    # 마지막 실제 입 모양도 끝 근처에 있어야 한다(뒤쪽이 비면 입이 먼저 멈춘다).
    assert frames[-2].at_ms > 4000 * 0.85


# --- 워커 ---------------------------------------------------------------


def _chunk(text: str, duration_ms: int, *, is_final: bool = False) -> SpeechChunk:
    return SpeechChunk(
        session_id="s1",
        tenant_id="acme",
        turn_id="s1:1",
        seq_in_turn=0,
        pcm=b"\x00\x00" * 100,
        sample_rate=24000,
        text=text,
        duration_ms=duration_ms,
        is_final=is_final,
    )


async def test_오디오_조각마다_타임라인이_나온다() -> None:
    bus = InMemoryEventBus()
    bus.register_group(Topic.AVATAR_TRACK, "probe")
    worker = AvatarWorker(bus, group="ava", consumer="ava-1")

    await worker.handle(_chunk("안녕하세요", 600))
    track: AvatarTrack = bus._queues[(Topic.AVATAR_TRACK, "probe")].get_nowait()

    assert track.state is AvatarState.SPEAKING
    assert track.duration_ms == 600
    assert track.frames and track.frames[-1].at_ms <= 600


async def test_식별자가_오디오와_일치한다() -> None:
    """클라이언트가 둘을 짝지어 재생한다. 어긋나면 동기가 통째로 깨진다."""
    bus = InMemoryEventBus()
    bus.register_group(Topic.AVATAR_TRACK, "probe")
    worker = AvatarWorker(bus, group="ava", consumer="ava-1")

    chunk = _chunk("안녕하세요", 600)
    await worker.handle(chunk)
    track: AvatarTrack = bus._queues[(Topic.AVATAR_TRACK, "probe")].get_nowait()

    assert (track.turn_id, track.seq_in_turn) == (chunk.turn_id, chunk.seq_in_turn)


async def test_발화가_끝나면_듣는_자세로_돌아간다() -> None:
    """마지막 입 모양이 남아 있으면 입을 벌린 채 굳어 있는 것처럼 보인다."""
    bus = InMemoryEventBus()
    bus.register_group(Topic.AVATAR_TRACK, "probe")
    worker = AvatarWorker(bus, group="ava", consumer="ava-1")

    await worker.handle(_chunk("", 0, is_final=True))
    track: AvatarTrack = bus._queues[(Topic.AVATAR_TRACK, "probe")].get_nowait()

    assert track.state is AvatarState.LISTENING
    assert track.frames == []
