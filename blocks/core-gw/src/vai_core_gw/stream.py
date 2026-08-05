"""``/v1/audio/stream`` WebSocket 종단.

와이어 규격(사양서 §4)과 내부 이벤트의 변환이 일어나는 유일한 지점이다.
클라이언트가 보낸 base64는 여기서 한 번만 풀고, 이후 파이프라인은 raw PCM으로
흐른다.

동시에 두 방향이 돌아간다:

* **수신 루프** — 클라이언트 → ``audio.in`` 발행
* **송신 태스크** — 세션 UI 채널 구독 → 클라이언트로 팬아웃

두 방향을 한 코루틴에서 처리하면 STT 결과를 보내는 동안 오디오 수신이 멈춘다.

다만 나눠 놓았다는 것은 **한쪽만 죽을 수 있다**는 뜻이기도 하다. 송신이 죽고
수신만 살아 있으면 오디오는 계속 올라가는데 화면은 영원히 멈춘다 — 클라이언트
입장에서는 "연결은 되어 있는데 자막이 안 나온다"이고, 그 상태로는 무엇을
해야 할지 알 수 없다. 그래서 송신이 끊기면 알리고 연결을 닫는다.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from vai_common.bus import EventBus
from vai_contracts.events import AudioChunk
from vai_contracts.session import Session, SessionState
from vai_contracts.topics import Topic
from vai_contracts.ws import AudioDataMessage, CloseSessionMessage, ErrorMessage

log = logging.getLogger(__name__)

MAX_CHUNK_BYTES = 1 << 20
"""청크 1건 상한(1MB ≒ 16kHz PCM 32초). 이보다 크면 오작동이거나 공격이다."""


class AudioStreamHandler:
    """WebSocket 연결 하나의 수명을 관리한다."""

    def __init__(self, websocket: WebSocket, session: Session, bus: EventBus) -> None:
        self._ws = websocket
        self._session = session
        self._bus = bus
        self._seq = 0

    async def run(self) -> None:
        forwarder = asyncio.create_task(self._forward_ui(), name="ui-forwarder")
        receiver = asyncio.create_task(self._receive_loop(), name="audio-receiver")
        try:
            # 어느 쪽이 먼저 끝나든 나머지를 접는다. 수신만 남기면 자막 없는
            # 통화가 계속되고, 송신만 남기면 아무도 말하지 않는 화면이 남는다.
            done, _ = await asyncio.wait({forwarder, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                # 예외를 꺼내 올린다. 태스크 안에서 조용히 죽는 것을 막는 지점이다.
                task.result()
        except WebSocketDisconnect:
            log.info("클라이언트 연결 종료", extra={"session_id": self._session.session_id})
        except Exception:
            # 이미 클라이언트에게 알린 뒤다. 여기서 프레임워크까지 예외를 올리면
            # Redis 순단 한 번이 스택트레이스로만 남고 원인은 더 안 보인다.
            log.exception("스트림 종료", extra={"session_id": self._session.session_id})
        finally:
            for task in (forwarder, receiver):
                task.cancel()
            # 취소 예외도, 이미 끝난 태스크의 예외도 여기서는 다시 다루지 않는다.
            for task in (forwarder, receiver):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _forward_ui(self) -> None:
        try:
            async for payload in self._bus.subscribe_ui(self._session.session_id):
                await self._ws.send_json(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 여기서 조용히 죽으면 "연결은 되어 있는데 자막만 안 나오는" 상태가
            # 남는다. 클라이언트가 재접속할 수 있도록 알리고 끝낸다.
            log.exception(
                "UI 송신 중단 — 연결을 닫는다",
                extra={"session_id": self._session.session_id},
            )
            with contextlib.suppress(Exception):
                await self._send_error("stream_broken", "결과 전송이 끊겼다 — 재접속이 필요하다")
            raise

    async def _receive_loop(self) -> None:
        while True:
            raw = await self._ws.receive_json()
            event = raw.get("event") if isinstance(raw, dict) else None

            if event == "audio_data":
                await self._on_audio(raw)
            elif event == "close_session":
                await self._on_close(raw)
                return
            else:
                await self._send_error("unknown_event", f"알 수 없는 이벤트: {event!r}")

    async def _on_audio(self, raw: dict[str, object]) -> None:
        try:
            message = AudioDataMessage.model_validate(raw)
        except ValidationError as exc:
            await self._send_error("invalid_message", f"메시지 형식 오류: {exc.error_count()}건")
            return

        # 토큰이 가리키는 세션 외의 세션으로 오디오를 밀어 넣지 못하게 한다.
        if message.session_id != self._session.session_id:
            await self._send_error("session_mismatch", "토큰과 다른 세션이다")
            return

        try:
            pcm = base64.b64decode(message.audio_base64, validate=True)
        except (binascii.Error, ValueError):
            await self._send_error("invalid_audio", "base64 디코딩 실패")
            return

        if not pcm:
            return
        if len(pcm) > MAX_CHUNK_BYTES:
            await self._send_error("chunk_too_large", f"청크 상한 {MAX_CHUNK_BYTES} 바이트 초과")
            return
        if len(pcm) % 2:
            # PCM 16bit는 항상 짝수 바이트다. 홀수면 프레임이 어긋나 잡음이 된다.
            await self._send_error("invalid_audio", "PCM 16bit 정렬이 맞지 않는다")
            return

        self._seq += 1
        await self._bus.publish(
            Topic.AUDIO_IN,
            AudioChunk(
                session_id=self._session.session_id,
                tenant_id=self._session.tenant_id,
                seq=self._seq,
                channel=message.channel,
                sample_rate=message.format.sample_rate,
                pcm=pcm,
                profile=self._session.profile,
            ),
        )

    async def _on_close(self, raw: dict[str, object]) -> None:
        try:
            message = CloseSessionMessage.model_validate(raw)
        except ValidationError:
            message = CloseSessionMessage(session_id=self._session.session_id)
        if message.session_id != self._session.session_id:
            await self._send_error("session_mismatch", "토큰과 다른 세션이다")
            return
        self._session.state = SessionState.CLOSING

    async def _send_error(self, code: str, message: str) -> None:
        log.warning("스트림 오류", extra={"session_id": self._session.session_id, "code": code})
        await self._ws.send_json(
            ErrorMessage(
                session_id=self._session.session_id, code=code, message=message
            ).model_dump(mode="json")
        )

    @property
    def should_close_session(self) -> bool:
        return self._session.state is SessionState.CLOSING
