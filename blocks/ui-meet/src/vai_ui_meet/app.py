"""UI-MEET — 스마트 회의록 화면.

회의 참가자가 보는 화면이다. 실시간 자막에 **화자 라벨이 뒤따라 붙고**,
회의 종료 후 안건·결정사항·Action Item이 정리된다.

**화자 라벨이 자막보다 늦게 오는 것을 전제로 설계했다.** 화자분리는 인식보다
느리므로, 라벨을 기다렸다가 자막을 그리면 회의 자막이 통째로 늦어진다.
자막을 먼저 띄우고 라벨이 도착하면 그 줄을 갱신한다 — 사람이 읽기에
"방금 누가 말했는지 이름이 붙는" 자연스러운 동작이다.

화자를 실명으로 바꾸는 것은 사람의 일이다. 화자분리는 "다른 사람"까지만
알려 주고 "누구인지"는 모른다. 참석자 이름 매핑 API가 그 간극을 메운다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis

from vai_common.bus import EventBus, RedisEventBus, build_bus
from vai_common.service import create_block_app, set_landing
from vai_common.settings import get_settings
from vai_contracts.events import SpeakerLabel
from vai_contracts.summary import Summary
from vai_contracts.topics import Topic

NAMES_KEY_PREFIX = "vai:meet:names:"

log = logging.getLogger(__name__)
BLOCK_ID = "UI-MEET"
STATIC_DIR = Path(__file__).parent / "static"


class MeetUiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_MEETUI_", extra="ignore")

    summary_url: str = "http://localhost:8089"
    diarization_url: str = "http://localhost:8093"

    gateway_url: str = "http://localhost:8080"
    """CORE-GW 주소. 회의 시작과 마이크 오디오가 이리로 간다.

    **브라우저가 아니라 이 프로세스가 부르는 주소다.** 화면은 늘 UI-MEET 만
    보고, 게이트웨이로 나가는 일은 여기서 대신한다. 브라우저가 직접 8080 을
    부르게 하면 포트가 둘로 늘어나고, 역방향 프록시 뒤나 다른 호스트에서
    바로 깨진다 — 그 사실은 고객사 망에서야 드러난다.
    """

    gateway_token: str = ""
    """CORE-GW 액세스 토큰. 게이트웨이가 dev_auth 면 비워 둔다.

    운영에서는 게이트웨이가 인증을 요구하므로 서비스 토큰이 필요하다.
    안 넣으면 회의 시작이 401 로 막힌다 — 화면에 그대로 보인다.
    """

    mic_enabled: bool = True
    """회의 화면에서 마이크로 직접 회의를 시작할 수 있게 한다.

    끄면 참관 전용 화면이 된다. 오디오가 PBX·회의 시스템에서 들어오는
    구성에서는 화면이 마이크를 잡을 이유가 없다.
    """


class SpeakerName(BaseModel):
    """화자 ID → 실명 매핑."""

    speaker_id: str
    display_name: str


class SpeakerNaming(BaseModel):
    session_id: str
    names: list[SpeakerName] = Field(default_factory=list)


def create_app(
    bus: EventBus | None = None,
    summary_client: httpx.AsyncClient | None = None,
    diarization_client: httpx.AsyncClient | None = None,
    gateway_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    common = get_settings()
    cfg = MeetUiSettings()
    injected = bus is not None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_bus = bus or build_bus(common.redis_url)
        application.state.bus = active_bus
        application.state.summary = summary_client or httpx.AsyncClient(
            base_url=cfg.summary_url.rstrip("/"), timeout=5.0
        )
        application.state.diarization = diarization_client or httpx.AsyncClient(
            base_url=cfg.diarization_url.rstrip("/"), timeout=5.0
        )
        application.state.gateway = gateway_client or httpx.AsyncClient(
            base_url=cfg.gateway_url.rstrip("/"),
            timeout=10.0,
            headers=({"authorization": f"Bearer {cfg.gateway_token}"} if cfg.gateway_token else {}),
        )
        # 참석자 실명은 **사람이 직접 입력한 데이터**다. 프로세스 메모리에 두면
        # 재기동으로 사라지고, 복제본이 둘이면 입력한 화면과 조회하는 화면이
        # 갈려 애초에 안 보인다. 회의록의 "누가 말했는가"가 speaker_1로 되돌아간다.
        application.state.names = (
            RedisSpeakerNames(active_bus.redis)
            if isinstance(active_bus, RedisEventBus)
            else InMemorySpeakerNames()
        )
        # 화자 라벨을 UI 채널로 중계하는 태스크들(세션별).
        application.state.relays = {}
        try:
            yield
        finally:
            for task in list(application.state.relays.values()):
                task.cancel()
            if not injected:
                await application.state.summary.aclose()
                await application.state.diarization.aclose()
                await application.state.gateway.aclose()
                await application.state.bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI UI-MEET", settings=common, lifespan=lifespan
    )

    @app.websocket("/v1/meet/stream")
    async def observe(websocket: WebSocket, session_id: str = "") -> None:
        """회의 관찰 소켓. 자막과 화자 라벨을 함께 흘린다."""
        if not session_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="session_id 필요")
            return

        await websocket.accept()
        bus_: EventBus = websocket.app.state.bus
        # 자막(UI 채널)과 화자 라벨(스트림 토픽)은 경로가 다르다. 두 갈래를
        # 한 소켓으로 합쳐 화면이 한 곳만 보게 한다.
        labels = asyncio.create_task(_relay_labels(bus_, websocket, session_id))
        subtitles = asyncio.create_task(_relay_subtitles(bus_, websocket, session_id))
        try:
            # 한쪽만 죽으면 화면이 조용히 반쪽이 된다 — 자막은 흐르는데 화자
            # 라벨이 영영 안 붙거나, 그 반대다. 어느 쪽이 끝나든 함께 접는다.
            done, _ = await asyncio.wait({labels, subtitles}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            log.info("회의 화면 종료", extra={"session_id": session_id})
        except Exception:
            log.exception("회의 관찰 소켓 오류", extra={"session_id": session_id})
        finally:
            for task in (labels, subtitles):
                task.cancel()
            for task in (labels, subtitles):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    @app.get("/v1/meet/summary/{session_id}", response_model=Summary, tags=["meeting"])
    async def get_summary(session_id: str, request: Request) -> Summary:
        client: httpx.AsyncClient = request.app.state.summary
        try:
            response = await client.get(f"/internal/v1/summaries/{session_id}")
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="요약 서비스에 연결할 수 없다"
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="회의록이 아직 없다")
        response.raise_for_status()
        return Summary.model_validate(response.json())

    @app.get("/v1/meet/speakers/{session_id}", tags=["meeting"])
    async def speakers(session_id: str, request: Request) -> list[dict[str, object]]:
        """참석자 목록(발언량 순) + 지정된 실명."""
        client: httpx.AsyncClient = request.app.state.diarization
        try:
            response = await client.get(f"/internal/v1/sessions/{session_id}/speakers")
        except httpx.HTTPError:
            # 화자분리가 꺼져 있거나 죽어도 회의 화면은 계속 동작해야 한다.
            log.warning("화자 조회 실패", exc_info=True)
            return []
        if response.status_code != httpx.codes.OK:
            return []

        names: dict[str, str] = await request.app.state.names.get(session_id)
        return [
            {**item, "display_name": names.get(str(item["speaker_id"]), "")}
            for item in response.json()
        ]

    @app.put("/v1/meet/speakers/{session_id}", response_model=SpeakerNaming, tags=["meeting"])
    async def name_speakers(
        session_id: str, payload: SpeakerNaming, request: Request
    ) -> SpeakerNaming:
        """화자에 실명을 붙인다.

        화자분리는 "다른 사람"까지만 알려 주고 "누구인지"는 모른다. 그 간극은
        사람이 메운다 — 자동 추정을 시도하면 회의록에 엉뚱한 이름이 박힌다.
        """
        payload.session_id = session_id
        await request.app.state.names.put(
            session_id, {item.speaker_id: item.display_name for item in payload.names}
        )
        return payload

    if cfg.mic_enabled:
        # 회의록 화면이 **회의를 시작할 수 있어야 한다.**
        #
        # 원래는 참관 전용이었다. 세션 ID 를 받아 자막만 흘려보는 화면이라,
        # 회의를 시작하려면 게이트웨이 데모 페이지를 먼저 열어 세션을 만들고
        # 그 ID 를 여기에 옮겨 적어야 했다. 고객사 담당자가 그 경로를 알 리
        # 없고, 실제로 회의 이름을 적어 넣고 "안 된다"로 끝났다.
        #
        # 게이트웨이는 이 프로세스가 대신 부른다. 화면은 UI-MEET 만 본다 —
        # 브라우저가 8080 을 직접 부르게 하면 역방향 프록시 뒤에서 깨진다.

        @app.post("/v1/meet/sessions", tags=["meeting"])
        async def open_meeting(request: Request) -> dict[str, str]:
            """회의 세션을 연다(회의록 프로파일 고정)."""
            client: httpx.AsyncClient = request.app.state.gateway
            try:
                response = await client.post(
                    "/v1/sessions",
                    json={"profile": "meeting", "audio_format": "pcm_16k", "language": "ko"},
                )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="게이트웨이에 연결할 수 없다 — CORE-GW 가 떠 있는지 본다",
                ) from exc
            return _gateway_session(response)

        @app.post("/v1/meet/sessions/{session_id}/join", tags=["meeting"])
        async def join_meeting(session_id: str, request: Request) -> dict[str, str]:
            """이미 도는 회의에 마이크를 하나 더 붙인다.

            회의는 여러 사람이 한다. 참여 길이 없으면 각자 켤 때마다 회의가
            새로 만들어지고, 회의록이 사람 수만큼 쪼개진다.
            """
            client: httpx.AsyncClient = request.app.state.gateway
            try:
                response = await client.post(f"/v1/sessions/{session_id}/join")
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, detail="게이트웨이에 연결할 수 없다"
                ) from exc
            return _gateway_session(response)

        @app.websocket("/v1/meet/mic")
        async def mic(websocket: WebSocket, token: str = "") -> None:
            """브라우저 마이크를 게이트웨이 오디오 소켓으로 중계한다.

            한 홉을 더 타는 대신 화면이 포트 하나만 보게 된다. 회의 오디오는
            초당 32KB 남짓이라 이 홉이 문제 되는 규모가 아니고, 대신
            역방향 프록시·다른 호스트·HTTPS 구성에서 그냥 동작한다.
            """
            if not token:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="token 필요")
                return
            await websocket.accept()
            await _relay_audio(cfg.gateway_url, token, websocket)

    @app.get("/minutes", include_in_schema=False)
    async def minutes() -> FileResponse:
        return FileResponse(STATIC_DIR / "minutes.html")

    @app.get("/v1/meet/capabilities", tags=["meeting"])
    async def capabilities() -> dict[str, bool]:
        """화면이 무엇을 할 수 있는지. 없는 버튼을 그려 놓고 눌리게 하지 않는다."""
        return {"mic": cfg.mic_enabled}

    set_landing(app, "/minutes")

    return app


class SpeakerNames(ABC):
    """화자 → 실명 매핑 저장소.

    화자분리는 "다른 사람"까지만 알려 주고 "누구인지"는 모른다. 그 간극을
    사람이 메우므로, 여기 담기는 것은 추정이 아니라 **입력된 사실**이다.
    잃어버리면 사용자가 다시 입력해야 한다.
    """

    @abstractmethod
    async def get(self, session_id: str) -> dict[str, str]: ...

    @abstractmethod
    async def put(self, session_id: str, names: dict[str, str]) -> None: ...


class InMemorySpeakerNames(SpeakerNames):
    """단일 프로세스용. 재기동하면 사라진다."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    async def get(self, session_id: str) -> dict[str, str]:
        return self._data.get(session_id, {})

    async def put(self, session_id: str, names: dict[str, str]) -> None:
        self._data[session_id] = names


class RedisSpeakerNames(SpeakerNames):
    """재기동과 복제를 넘어 남는다.

    요약과 같은 수명을 준다 — 회의록을 여는 한 참석자 이름도 함께 있어야 한다.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, session_id: str) -> str:
        return f"{NAMES_KEY_PREFIX}{session_id}"

    async def get(self, session_id: str) -> dict[str, str]:
        raw = await self._redis.hgetall(self._key(session_id))
        return {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in raw.items()
        }

    async def put(self, session_id: str, names: dict[str, str]) -> None:
        key = self._key(session_id)
        pipe = self._redis.pipeline()
        pipe.delete(key)
        # 필드별로 넣는다. 참석자는 많아야 열몇 명이라 파이프라인 한 번에
        # 다 들어가고, mapping 인자의 타입을 우회하려 캐스팅할 이유가 없다.
        for speaker_id, display_name in names.items():
            pipe.hset(key, speaker_id, display_name)
        await pipe.execute()


async def _relay_subtitles(bus: EventBus, websocket: WebSocket, session_id: str) -> None:
    """UI 채널(자막·요약)을 소켓으로 넘긴다."""
    async for payload in bus.subscribe_ui(session_id):
        await websocket.send_json(payload)


async def _relay_labels(bus: EventBus, websocket: WebSocket, session_id: str) -> None:
    """화자 라벨 토픽을 구독해 이 세션 것만 소켓으로 넘긴다.

    화면마다 별도 컨슈머 그룹을 쓴다. 같은 그룹을 공유하면 화면 두 개가
    라벨을 나눠 갖게 되어 각자 절반만 보인다.
    """
    group = f"ui-meet:{session_id}"
    try:
        async for delivery in bus.consume(
            Topic.SPEAKER_LABEL, SpeakerLabel, group=group, consumer="screen"
        ):
            await delivery.ack()
            label = delivery.event
            if label.session_id != session_id:
                continue
            await websocket.send_json({"event": "speaker_label", **label.model_dump(mode="json")})
    except asyncio.CancelledError:
        raise
    except Exception:
        # 조용히 죽으면 자막은 흐르는데 화자 라벨만 영영 안 붙는다. 회의록에서
        # "누가 말했는가"가 통째로 비는 상태이고, 화면은 정상으로 보인다.
        log.exception("화자 라벨 중계 중단", extra={"session_id": session_id})
        raise


def _gateway_session(response: httpx.Response) -> dict[str, str]:
    """게이트웨이 응답을 화면이 쓸 두 값으로 줄인다.

    실패하면 **게이트웨이가 말한 이유를 그대로 올린다.** 여기서 "회의를
    시작할 수 없다"로 뭉개면, 끝난 세션이라 막힌 것인지 인증이 없어 막힌
    것인지 화면에서 구분할 수 없다.
    """
    if response.status_code >= httpx.codes.BAD_REQUEST:
        detail = "회의를 시작할 수 없다"
        with contextlib.suppress(Exception):
            detail = response.json().get("detail") or detail
        raise HTTPException(response.status_code, detail=detail)

    payload = response.json()
    return {
        "session_id": str(payload["session"]["session_id"]),
        "ws_token": str(payload["ws_token"]),
    }


async def _relay_audio(gateway_url: str, token: str, browser: WebSocket) -> None:
    """브라우저 → 게이트웨이 오디오 중계.

    되돌아오는 자막은 여기로 흘리지 않는다. 회의 화면은 이미 관찰 소켓으로
    자막을 받고 있어서, 양쪽으로 보내면 같은 줄이 두 번 그려진다.
    """
    target = gateway_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    try:
        async with websockets.connect(f"{target}/v1/audio/stream?token={token}") as upstream:
            while True:
                frame = await browser.receive_text()
                await upstream.send(frame)
    except WebSocketDisconnect:
        log.info("회의 마이크 종료")
    except Exception:
        # 조용히 끊기면 화면은 "연결됨"인데 자막이 영영 안 나온다.
        log.exception("마이크 중계 중단")
        with contextlib.suppress(Exception):
            await browser.close(code=status.WS_1011_INTERNAL_ERROR, reason="게이트웨이 연결 끊김")
