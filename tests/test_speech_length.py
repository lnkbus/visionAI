"""잡음에서 참석자가 생기지 않는가.

맥 시연에서 회의를 시작하고 몇 초 만에 참석자 목록이 이렇게 찼다:

    화자 2 — 3초 · 1회
    화자 4 — 2초 · 1회
    화자 6 — 2초 · 1회
    ...  일곱 명

아무도 말하지 않았다. 방 소음이었다.

원인은 길이를 재는 자가 둘이라는 데 있었다.

* ``duration_ms`` — 앞뒤 패딩(200ms)과 행오버(400ms)가 포함된 **구간 길이**
* ``speech_ms``  — 그 안에서 **음성으로 판정된 프레임만** 합친 길이

AUD-VAD 는 둘을 나눠 계산해 놓고 `speech_ms` 를 이벤트에 안 실었다. 받는
SPK-DIA 는 없는 줄 모르고 `duration_ms` 로 "짧으면 새 화자로 안 친다"를
판정했는데, **패딩만으로 600ms 라 그 조건이 한 번도 안 걸린다.** 기침 하나가
새 참석자가 된다.

세그멘터 주석에는 이 위험이 그대로 적혀 있었다("그 값으로 거르면 아무것도
걸러지지 않는다"). 블록 경계를 넘으면서 지켜지지 않았을 뿐이다 — 계약에
필드가 없으면 받는 쪽은 그런 구분이 있다는 사실 자체를 모른다.

회의록에 참석하지 않은 사람이 생기는 것이 왜 나쁘냐면, **아무도 못
알아보기 때문이다.** 발언이 엉뚱한 사람에게 붙고 그대로 공유된다.
"""

from __future__ import annotations

import numpy as np
import pytest

from vai_aud_vad.adapters.energy import EnergyVadAdapter
from vai_aud_vad.segmenter import SegmenterConfig, SpeechSegmenter
from vai_contracts.events import AudioSegment
from vai_contracts.session import ChannelRole, SessionProfile
from vai_spk_dia.diarizer import DiarizerConfig, OnlineDiarizer

RATE = 16000


def _pcm(seconds: float, amplitude: int) -> bytes:
    count = int(RATE * seconds)
    wave = (np.sin(np.arange(count) * 0.07) * amplitude).astype("<i2")
    return wave.tobytes()


def test_계약에_말한_길이가_있다() -> None:
    """없으면 받는 쪽은 그런 구분이 있다는 사실 자체를 모른다."""
    assert "speech_ms" in AudioSegment.model_fields


def test_짧은_잡음은_구간_길이가_길게_잡힌다() -> None:
    """이 시험이 사고의 핵심이다. 실제 발화가 짧아도 패딩·행오버 때문에
    구간은 길어진다 — 그래서 구간 길이로 거르면 아무것도 안 걸러진다."""
    segmenter = SpeechSegmenter(EnergyVadAdapter(), RATE, SegmenterConfig())

    segments = []
    segments += segmenter.push("s", _pcm(0.12, 9000))  # 짧은 소리 하나
    segments += segmenter.push("s", _pcm(1.0, 0))  # 무음으로 구간을 닫는다
    finals = [item for item in segments if item.is_final]

    assert finals, "구간이 안 닫혔다 — 이 시험이 무의미해진다"
    segment = finals[0]
    assert segment.speech_ms < segment.duration_ms, "두 길이가 같으면 구분이 없어진 것이다"
    assert segment.duration_ms > 400, (
        f"구간 길이가 {segment.duration_ms}ms 다 — 패딩·행오버가 안 붙었다면 "
        "이 시험의 전제가 낡았다"
    )


def test_말한_길이가_이벤트에_실린다() -> None:
    """AUD-VAD 가 계산해 놓고 안 실으면 SPK-DIA 는 없는 줄 모른다."""
    import inspect

    from vai_aud_vad import worker

    source = inspect.getsource(worker)

    assert "speech_ms=segment.speech_ms" in source, (
        "VAD 가 말한 길이를 이벤트에 안 싣는다 — 받는 쪽이 구간 길이로 판정하게 된다"
    )


def test_화자분리는_말한_길이로_판정한다() -> None:
    import inspect

    from vai_spk_dia import worker

    source = inspect.getsource(worker)

    assert "event.speech_ms" in source, (
        "구간 길이로 판정하면 패딩·행오버 때문에 짧은 잡음이 전부 새 화자가 된다"
    )


@pytest.mark.parametrize("speech_ms", [0, 50, 200, 690])
def test_짧게_말한_것으로는_새_화자를_안_만든다(speech_ms: int) -> None:
    """기침·키보드·문 닫는 소리가 매번 새 참석자가 되면 회의록이 못 쓰게 된다."""
    diarizer = OnlineDiarizer(DiarizerConfig(min_duration_ms=700))
    rng = np.random.default_rng(0)

    created = 0
    for _ in range(8):
        # 잡음이라 매번 전혀 다른 임베딩이 나온다 — 실제로 이 상황이었다.
        vector = rng.normal(size=32).astype(np.float32)
        vector /= np.linalg.norm(vector)
        assignment = diarizer.assign(vector, speech_ms)
        created += bool(assignment and assignment.is_new)

    assert created <= 1, f"짧은 잡음 8건에서 화자가 {created}명 생겼다"


def test_충분히_말하면_화자가_생긴다() -> None:
    """거르는 쪽만 시험하면 "아무도 안 잡히는" 구성을 통과시킨다."""
    diarizer = OnlineDiarizer(DiarizerConfig(min_duration_ms=700))
    rng = np.random.default_rng(1)

    created = 0
    for _ in range(3):
        vector = rng.normal(size=32).astype(np.float32)
        vector /= np.linalg.norm(vector)
        assignment = diarizer.assign(vector, 1500)
        created += bool(assignment and assignment.is_new)

    assert created >= 2, "제대로 말한 발화에서도 화자가 안 생긴다"


def test_옛_producer_는_구간_길이로_물러선다() -> None:
    """speech_ms 가 0 이면 안 채운 것이다. 그때까지 판정을 포기하면 화자분리가
    통째로 멈춘다 — 섞여 도는 구간에서는 그게 더 나쁘다."""
    segment = AudioSegment(
        session_id="s",
        tenant_id="t",
        seq=1,
        channel=ChannelRole.CUSTOMER,
        sample_rate=RATE,
        pcm=b"",
        profile=SessionProfile.MEETING,
        start_ms=0,
        duration_ms=1500,
    )

    assert segment.speech_ms == 0
    assert (segment.speech_ms or segment.duration_ms) == 1500
