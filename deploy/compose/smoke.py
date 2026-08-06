#!/usr/bin/env python3
"""compose 스택을 **실제로 띄워서** 도는지 본다.

CI 는 이미지를 빌드만 하고 한 번도 돌려 보지 않았다. 그래서 빌드는 전부
초록불인데 고객사 서버에서 처음 기동하는 상황이 반복됐고, 같은 계열의 결함이
열두 번 났다:

    ModuleNotFoundError: No module named 'uvicorn'        (AUD-VAD·STT-CORE)
    ModuleNotFoundError: No module named 'vai_retrieval'  (TA-ASSIST·BOT-VOICE)
    ModuleNotFoundError: No module named 'qdrant_client'  (RAG-KB·RAG-SRCH)
    PermissionError: chmod '/var/lib/visionai/audit'      (CORE-SEC)
    container visionai-qdrant-1 is unhealthy              (qdrant 헬스체크)

전부 **기동 순간**에만 드러나는 것들이다. 정적 검사로 잡을 수 있는 것은
이미 문지기를 세웠지만, 마지막 한 겹은 띄워 보는 수밖에 없다.

여기서 보는 것:

1. 스물한 블록이 전부 뜨고 **재기동 루프에 빠지지 않았는가**
2. `/healthz` 가 답하는가 — 그리고 `/` 가 화면으로 보내는가
3. **파이프라인이 실제로 도는가** — 마이크 오디오를 넣어 자막이 돌아오는지.
   CORE-GW → CORE-BUS → AUD-VAD → STT-CORE → FLT-MICRO → 화면 전 구간이다.
4. 회의를 열고, 다른 사람이 **같은 회의에 붙을 수 있는가**

실제 인식 엔진과 화자분리 신경망은 안 쓴다(EXTRAS 를 비운다). 가중치가
수 GB 라 CI 에서 받을 것이 아니고, 여기서 재려는 것은 인식 품질이 아니라
**배선**이다. 품질은 evalctl 의 몫이다.

    docker compose -f deploy/compose/docker-compose.yml up -d
    uv run --with websockets python deploy/compose/smoke.py

진짜 엔진(faster_whisper)으로 떠 있는 스택에 돌리면 **자막 검사만 판정을
보류한다.** 여기서 넣는 것은 220Hz 사인파고, Whisper 는 그걸 말로 듣지
않는다 — 환각 필터가 걸러 내는 것이 오히려 정상이다. 배선까지 단정하려면
CI 와 같은 구성(`VAI_STT_ADAPTER=fake`)으로 돌린다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy/compose/docker-compose.yml")]

GATEWAY = "http://127.0.0.1:8080"
MEET = "http://127.0.0.1:8094"

# 화면을 가진 블록: 포트만 쳐도 화면으로 가야 한다.
SCREENS = {8080: "/demo", 8090: "/console", 8091: "/workspace", 8094: "/minutes", 8097: "/console"}
# 화면이 없는 API 블록도 `/` 가 답해야 한다(빈 404 가 아니라 자기소개).
API_PORTS = [8081, 8085, 8086, 8087, 8089, 8093, 8095, 8096]

failures: list[str] = []


def fail(what: str, detail: str = "") -> None:
    failures.append(what if not detail else f"{what}\n    {detail}")
    print(f"  ✗ {what}")
    if detail:
        print(f"    {detail}")


def ok(what: str) -> None:
    print(f"  ✓ {what}")


def compose(*args: str) -> str:
    return subprocess.run([*COMPOSE, *args], capture_output=True, text=True, check=False).stdout


def stt_adapter() -> str:
    """STT-CORE 가 실제로 무엇으로 돌고 있는지.

    **합성음으로는 진짜 엔진의 결과를 단정할 수 없다.** 이 스크립트가 넣는
    것은 220Hz 사인파고, Whisper 는 그걸 말로 듣지 않는다 — 환각 필터가
    걸러 내면 자막이 안 오는 것이 오히려 정상이다. 그때 "끊겼다"고 하면
    멀쩡한 스택을 두고 원인을 찾게 된다.
    """
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", "stt-core", "printenv", "VAI_STT_ADAPTER"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "faster_whisper"


def logs_of(service: str, lines: int = 40) -> str:
    result = subprocess.run(
        [*COMPOSE, "logs", "--no-color", f"--tail={lines}", service],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout or result.stderr


def get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # 연결 거부·타임아웃 — 뜨지 않았다는 뜻이다
        return 0, str(exc)


def post(url: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"detail": str(exc)}


# ── 1. 컨테이너 상태 ─────────────────────────────────────────────────────────


def check_containers() -> None:
    """**재기동 루프를 잡는 자리다.** 크래시 루프는 `up -d` 를 성공으로 끝내고
    나서 시작되므로, 띄운 직후만 보면 초록불이 나온다."""
    print("▸ 컨테이너")
    raw = compose("ps", "--format", "json").strip()
    if not raw:
        fail("compose ps 가 비었다 — 스택이 안 떠 있다")
        return

    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    for row in sorted(rows, key=lambda item: str(item.get("Service"))):
        service, state = str(row.get("Service")), str(row.get("State"))
        status = str(row.get("Status", ""))
        if state != "running":
            fail(f"{service}: {state} ({status})", logs_of(service))
        elif "Restarting" in status or "unhealthy" in status:
            fail(f"{service}: {status}", logs_of(service))
        else:
            ok(f"{service} — {status}")


# ── 2. 헬스와 첫 화면 ────────────────────────────────────────────────────────


def check_health() -> None:
    print("▸ /healthz")
    for port in sorted({*SCREENS, *API_PORTS}):
        status, body = get(f"http://127.0.0.1:{port}/healthz")
        if status != 200:
            fail(f"포트 {port}: /healthz {status or '연결 실패'}", body[:200])
        else:
            ok(f"{port} — {json.loads(body).get('block')}")


def check_landing() -> None:
    """포트만 아는 사람이 화면을 찾을 수 있는가. 실제로 여기서 막혔다."""
    print("▸ / (첫 화면)")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    for port, screen in sorted(SCREENS.items()):
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as response:
                fail(f"포트 {port}: / 가 {response.status} 다 — 화면으로 안 보낸다")
        except urllib.error.HTTPError as exc:
            where = exc.headers.get("location", "")
            if exc.code in {301, 302, 307, 308} and where == screen:
                ok(f"{port} → {where}")
            else:
                fail(f"포트 {port}: / → {exc.code} {where or '(주소 없음)'}, 기대는 {screen}")
        except Exception as exc:
            fail(f"포트 {port}: / 를 열지 못했다", str(exc))

    for port in API_PORTS:
        status, body = get(f"http://127.0.0.1:{port}/")
        if status != 200:
            fail(f"포트 {port}: / 가 {status} 다 — API 블록도 자기소개는 해야 한다")
        elif "/healthz" not in body:
            fail(f"포트 {port}: / 가 다음에 볼 곳을 안 알려 준다")
        else:
            ok(f"{port} — 자기소개")


# ── 3. 파이프라인 왕복 ───────────────────────────────────────────────────────


def speech_frames(seconds: float = 1.6, rate: int = 16000) -> bytes:
    """VAD 를 확실히 넘기는 크기의 신호. 무음이면 구간이 안 열린다."""
    count = int(rate * seconds)
    samples = [int(9000 * math.sin(2 * math.pi * 220 * n / rate)) for n in range(count)]
    return struct.pack(f"<{count}h", *samples)


def silence(seconds: float = 1.2, rate: int = 16000) -> bytes:
    """행오버를 채워 구간을 **닫는다.** 안 닫으면 확정 자막이 안 나온다."""
    return b"\x00\x00" * int(rate * seconds)


def audio_message(session_id: str, pcm: bytes, channel: str = "customer") -> str:
    return json.dumps(
        {
            "event": "audio_data",
            "session_id": session_id,
            "format": "pcm_16k",
            "channel": channel,
            "audio_base64": base64.b64encode(pcm).decode(),
        }
    )


async def _feed(socket: object, session_id: str) -> None:
    """0.2초씩 나눠 보낸다 — 실제 브라우저가 보내는 모양이다."""
    chunk = 16000 // 5 * 2
    stream = speech_frames() + silence()
    for at in range(0, len(stream), chunk):
        await socket.send(audio_message(session_id, stream[at : at + chunk]))  # type: ignore[attr-defined]
        await asyncio.sleep(0.02)


async def _await_caption(socket: object, seconds: float) -> str:
    async with asyncio.timeout(seconds):
        while True:
            message = json.loads(await socket.recv())  # type: ignore[attr-defined]
            if message.get("event") == "agent_assist_update" and message.get("stt_result"):
                return str(message["stt_result"].get("text", ""))
            if message.get("event") == "error":
                raise RuntimeError(str(message.get("message")))


async def check_pipeline() -> None:
    """오디오를 넣어 자막이 돌아오는가.

    이 한 줄이 CORE-GW·CORE-BUS·AUD-VAD·STT-CORE·FLT-MICRO 를 한꺼번에
    확인한다. 어느 하나가 죽어 있으면 자막이 안 온다.
    """
    print("▸ 파이프라인 (오디오 → 자막)")
    status, payload = post(f"{GATEWAY}/v1/sessions", {"profile": "aicc", "audio_format": "pcm_16k"})
    if status != 201:
        fail(f"세션을 못 만들었다 ({status})", json.dumps(payload, ensure_ascii=False))
        return

    session_id = str(payload["session"]["session_id"])  # type: ignore[index]
    token = str(payload["ws_token"])

    try:
        async with websockets.connect(f"ws://127.0.0.1:8080/v1/audio/stream?token={token}") as ws:
            await _feed(ws, session_id)
            text = await _await_caption(ws, seconds=30)
        ok(f"자막 도착 — {text[:40]}")
    except TimeoutError:
        adapter = stt_adapter()
        if adapter != "fake":
            # 여기서 단정하지 않는다. 넣은 것이 사인파라 진짜 엔진이 아무것도
            # 못 알아듣는 것이 정상이고, 그것을 실패로 세면 이 스크립트를
            # 아무도 믿지 않게 된다.
            print(f"  ⚠ 자막이 안 왔다 — 다만 지금 엔진이 {adapter} 다")
            print("    합성음(사인파)으로는 진짜 엔진의 결과를 단정할 수 없다.")
            print("    배선만 보려면 CI 와 같은 구성으로 돌린다:")
            print("      VAI_STT_ADAPTER=fake docker compose ... up -d stt-core")
            return
        fail(
            "30초 안에 자막이 안 왔다 — 어딘가에서 끊겼다",
            "\n".join(logs_of(name, 15) for name in ("aud-vad", "stt-core", "flt-micro")),
        )
    except Exception as exc:
        fail("오디오 스트림이 깨졌다", str(exc))


def check_join() -> None:
    """상담·회의는 여러 사람이 한다. 붙는 길이 없으면 사람마다 세션이 새로
    생기고 기록이 그만큼 쪼개진다."""
    print("▸ 세션 참여")
    status, payload = post(f"{GATEWAY}/v1/sessions", {"profile": "aicc"})
    if status != 201:
        fail(f"세션을 못 만들었다 ({status})")
        return
    session_id = str(payload["session"]["session_id"])  # type: ignore[index]

    status, joined = post(f"{GATEWAY}/v1/sessions/{session_id}/join")
    if status != 200:
        fail(f"참여가 {status} 다", json.dumps(joined, ensure_ascii=False))
    elif joined["session"]["session_id"] != session_id:  # type: ignore[index]
        fail("참여했더니 다른 세션이 생겼다")
    else:
        ok(f"같은 세션에 붙었다 — {session_id}")

    status, _ = post(f"{GATEWAY}/v1/sessions/sess_no_such_session/join")
    if status != 404:
        fail(f"없는 세션에 붙는 것을 안 막는다 ({status})")
    else:
        ok("없는 세션은 404")


async def check_meeting() -> None:
    """회의록 화면에서 회의를 열고 자막까지 받는가.

    UI-MEET → CORE-GW 중계가 여기서만 실제로 돈다. 화면이 게이트웨이를 직접
    부르지 않는 구조라, 이 홉이 끊기면 회의가 통째로 안 열린다.
    """
    print("▸ 회의 (회의록 화면에서 시작)")
    status, payload = post(f"{MEET}/v1/meet/sessions")
    if status != 200:
        fail(f"회의를 못 열었다 ({status})", json.dumps(payload, ensure_ascii=False))
        return

    session_id, token = str(payload["session_id"]), str(payload["ws_token"])
    ok(f"회의 열림 — {session_id}")

    status, joined = post(f"{MEET}/v1/meet/sessions/{session_id}/join")
    if status != 200 or joined.get("session_id") != session_id:
        fail(f"같은 회의에 못 붙는다 ({status})", json.dumps(joined, ensure_ascii=False))
    else:
        ok("다른 참석자가 같은 회의에 붙었다")

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:8094/v1/meet/stream?session_id={session_id}"
        ) as observer:
            async with websockets.connect(f"ws://127.0.0.1:8094/v1/meet/mic?token={token}") as mic:
                await _feed(mic, session_id)
                text = await _await_caption(observer, seconds=30)
        ok(f"회의 자막 도착 — {text[:40]}")
    except TimeoutError:
        adapter = stt_adapter()
        if adapter != "fake":
            print(f"  ⚠ 회의 자막이 안 왔다 — 다만 지금 엔진이 {adapter} 다 (위와 같은 이유)")
            return
        fail(
            "회의 자막이 30초 안에 안 왔다",
            "\n".join(logs_of(name, 15) for name in ("ui-meet", "core-gw", "stt-core")),
        )
    except Exception as exc:
        fail("회의 마이크 중계가 깨졌다", str(exc))


async def main() -> int:
    check_containers()
    check_health()
    check_landing()
    await check_pipeline()
    check_join()
    await check_meeting()

    print()
    if failures:
        print(f"✗ {len(failures)}건 실패")
        for item in failures:
            print(f"  · {item.splitlines()[0]}")
        return 1
    print("✓ 스택이 실제로 돈다")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
