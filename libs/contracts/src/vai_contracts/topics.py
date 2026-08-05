"""이벤트 버스 토픽 정의.

두 가지 전달 방식을 구분한다.

* **Stream 토픽** — 내구성 있는 처리 경로. Redis Streams + 컨슈머 그룹으로
  워커 간 부하 분산이 이뤄진다. 토픽은 타입별로 하나이며 세션은 메시지 필드
  (``session_id``)로 구분한다. 세션마다 스트림을 만들면 워커가 신규 세션
  스트림을 발견할 방법이 없기 때문이다.
* **UI 채널** — 접속 중인 화면으로의 즉시 팬아웃. Redis Pub/Sub을 쓰며
  세션별 채널을 사용한다(구독자는 자기 세션만 받으면 된다).

블록은 이 모듈의 상수만 참조하고 문자열을 직접 만들지 않는다.
"""

from __future__ import annotations

from enum import StrEnum

NAMESPACE = "vai"


class Topic(StrEnum):
    """Stream 토픽 (durable, consumer-group)."""

    AUDIO_IN = "audio.in"
    """AUD-RTP / AUD-WS → AUD-VAD. 원시 오디오 청크."""

    AUDIO_SEGMENT = "audio.segment"
    """AUD-VAD → STT-CORE. VAD가 확정한 발화 구간."""

    STT_DELTA = "stt.delta"
    """STT-CORE → FLT-MICRO. 스트리밍 인식 결과(부분/최종)."""

    SPEAKER_LABEL = "speaker.label"
    """SPK-DIA → LLM-SUM / UI-MEET. 발화 구간의 화자 식별 결과.

    자막 경로와 **분리**한 이유: 화자분리는 인식보다 느리고 정확도도 낮다.
    자막이 화자 라벨을 기다리면 회의 자막이 통째로 늦어진다. 라벨은 뒤따라
    붙는다 — 회의록(요약)은 어차피 종료 후 만들어지므로 그때까지만 모이면 된다."""

    FILTER_CLEAN = "filter.clean"
    """FLT-MICRO → TA-ASSIST / LLM-SUM. 마스킹 완료 + 룰 매칭 결과."""

    ASSIST_POPUP = "assist.popup"
    """TA-ASSIST → UI. 지식 팝업 추천."""

    SESSION_CLOSED = "session.closed"
    """CORE-BUS → LLM-SUM. 배치 요약 트리거."""

    SUMMARY_DONE = "summary.done"
    """LLM-SUM → UI. 요약 완료."""

    TTS_REQUEST = "tts.request"
    """BOT-VOICE / TA-ASSIST → TTS-CORE. 합성 요청."""

    TTS_AUDIO = "tts.audio"
    """TTS-CORE → AUD-RTP / UI. 합성된 오디오 조각(문장 단위).

    전체 합성을 기다렸다 보내면 첫 소리가 문장 수만큼 늦어진다. 사람은 상대가
    0.5초 안에 반응하지 않으면 다시 말하기 시작한다."""

    TTS_CANCEL = "tts.cancel"
    """끼어들기. 고객이 말을 시작했는데 봇이 계속 떠들면 그 통화는 실패한다.

    **Pub/Sub이 아니라 스트림인 이유**: 취소가 유실되면 봇이 멈추지 않는다.
    화면 갱신이 한 번 빠지는 것과 무게가 다르다."""

    BOT_TURN = "bot.turn"
    """BOT-VOICE → UI / LLM-SUM / 감사. 봇이 한 마디 했다는 기록.

    봇이 무슨 말을 했는지 남지 않으면 분쟁이 생겼을 때 아무것도 증명할 수 없다."""

    AVATAR_TRACK = "avatar.track"
    """AVA-COUNSEL → 아바타 클라이언트. 오디오 조각에 대응하는 입 모양 타임라인.

    영상이 아니라 타임라인만 보낸다 — 세션마다 렌더링하면 GPU가 통화 수만큼
    필요하고, 인코딩·전송 지연이 음성 위에 더해진다."""

    AUDIT_LOG = "audit.log"
    """모든 블록 → CORE-SEC. 감사 기록.

    버스를 경유하는 이유: 감사 저장이 상담 응답 경로에 동기적으로 끼면
    저장소가 느려질 때 상담이 느려진다. 단, 접근 통제에 관한 기록(PII 열람 등)은
    **기록 성공 후 열람 허용**이어야 하므로 그쪽은 CORE-SEC의 동기 API를 쓴다."""


def stream(topic: Topic) -> str:
    """토픽의 Redis Stream 키."""
    return f"{NAMESPACE}:{topic.value}"


def ui_channel(session_id: str) -> str:
    """세션별 UI Pub/Sub 채널 키."""
    return f"{NAMESPACE}:ui:{session_id}"
