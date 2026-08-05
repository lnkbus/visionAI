"""스택 스모크 테스트 — 네 블록을 별도 프로세스로 띄우고 실제로 왕복시킨다.

인메모리 통합 테스트(:mod:`tests.test_pipeline_e2e`)가 검증하지 못하는 것들이
여기서 걸린다: Redis Streams 컨슈머 그룹 배선, 블록 간 HTTP 계약, WebSocket
핸드셰이크, base64 왕복, 그리고 각 블록이 실제로 기동하는지 자체.

Redis가 없으면 건너뛴다 — 개발자 기본 루프를 막지 않기 위해서다.
CI는 Redis를 띄워 이 테스트까지 돌린다.
"""

from __future__ import annotations

import base64
import json
import math
import os
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REDIS_URL = os.environ.get("VAI_TEST_REDIS_URL", "redis://localhost:6379/9")
SAMPLE_RATE = 16000

websockets = pytest.importorskip("websockets.sync.client", reason="websockets 미설치")


def _redis_available() -> bool:
    try:
        import redis

        redis.Redis.from_url(REDIS_URL).ping()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis가 필요하다")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise TimeoutError(f"기동 실패: {url}")


def speech_pcm(duration_ms: int, *, amplitude: int = 12000) -> bytes:
    count = int(SAMPLE_RATE * duration_ms / 1000)
    return struct.pack(
        f"<{count}h",
        *(int(amplitude * math.sin(2 * math.pi * 220 * n / SAMPLE_RATE)) for n in range(count)),
    )


def silence_pcm(duration_ms: int) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * duration_ms / 1000)


def spawn(module: str, env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


@pytest.fixture(scope="module")
def stack() -> Iterator[dict[str, int]]:
    """CORE-BUS · CORE-GW · AUD-VAD · STT-CORE를 실제 프로세스로 띄운다."""
    import redis

    redis.Redis.from_url(REDIS_URL).flushdb()

    bus_port, gw_port, vad_port, stt_port = (free_port() for _ in range(4))
    base = {
        "VAI_REDIS_URL": REDIS_URL,
        "VAI_CORE_BUS_URL": f"http://127.0.0.1:{bus_port}",
        "VAI_LOG_LEVEL": "WARNING",
    }
    procs = [
        spawn("vai_core_bus.main", {**base, "VAI_PORT": str(bus_port)}),
        spawn("vai_core_gw.main", {**base, "VAI_PORT": str(gw_port)}),
        spawn(
            "vai_aud_vad.main",
            {**base, "VAI_PORT": str(vad_port), "VAI_CONSUMER_GROUP": "vad"},
        ),
        spawn(
            "vai_stt_core.main",
            {**base, "VAI_PORT": str(stt_port), "VAI_CONSUMER_GROUP": "stt"},
        ),
    ]
    try:
        for port in (bus_port, gw_port, vad_port, stt_port):
            wait_for_http(f"http://127.0.0.1:{port}/healthz")
        yield {"gw": gw_port, "bus": bus_port}
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_microphone_audio_becomes_a_caption(stack: dict[str, int]) -> None:
    """데모 페이지가 하는 일과 정확히 같은 순서를 코드로 재현한다."""
    gw = f"http://127.0.0.1:{stack['gw']}"

    created = httpx.post(f"{gw}/v1/sessions", json={"profile": "aicc"}, timeout=10.0)
    assert created.status_code == 201, created.text
    body = created.json()
    session_id = body["session"]["session_id"]

    url = f"ws://127.0.0.1:{stack['gw']}/v1/audio/stream?token={body['ws_token']}"
    with websockets.connect(url, open_timeout=10) as ws:
        for pcm in (speech_pcm(700), silence_pcm(800)):
            ws.send(
                json.dumps(
                    {
                        "event": "audio_data",
                        "session_id": session_id,
                        "format": "pcm_16k",
                        "channel": "customer",
                        "audio_base64": base64.b64encode(pcm).decode(),
                    }
                )
            )

        deadline = time.monotonic() + 20
        caption = None
        while caption is None and time.monotonic() < deadline:
            message = json.loads(ws.recv(timeout=20))
            if message.get("event") == "agent_assist_update" and message.get("stt_result"):
                caption = message["stt_result"]

    assert caption is not None, "자막이 도착하지 않았다"
    assert caption["text"], "빈 자막이 왔다"
    assert caption["speaker"] == "customer"


def test_session_close_is_recorded(stack: dict[str, int]) -> None:
    gw = f"http://127.0.0.1:{stack['gw']}"
    session_id = httpx.post(f"{gw}/v1/sessions", json={"profile": "meeting"}, timeout=10.0).json()[
        "session"
    ]["session_id"]

    closed = httpx.post(f"{gw}/v1/sessions/{session_id}/close", timeout=10.0)

    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"
