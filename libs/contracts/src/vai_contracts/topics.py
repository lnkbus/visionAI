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
