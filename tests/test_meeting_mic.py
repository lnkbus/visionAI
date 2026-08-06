"""회의록 화면에서 **회의를 시작할 수 있는가.**

이 화면은 참관 전용이었다. 세션 ID 를 받아 자막만 흘려보는 곳이라, 회의를
시작하려면 게이트웨이 데모 페이지를 먼저 열어 세션을 만들고 그 ID 를 여기에
옮겨 적어야 했다. 그 경로는 어디에도 안 적혀 있었고, 실제로 회의 이름을
적어 넣고 "회의록이 작동을 안 한다"로 끝났다.

**여러 사람이 붙을 수 있는가**도 같은 뿌리의 문제다. 세션을 만드는 길만
있으면 화면 하나가 곧 세션 하나가 된다 — 두 사람이 각자 마이크를 켜면 서로
다른 회의 두 건이 되고, 회의록이 인원수만큼 쪼개진다. 상담 데모에서
드러난 모습은 이랬다: 두 사람이 말하는데 화자가 전부 "고객"이었다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vai_core_gw.app import create_app as create_gateway
from vai_ui_meet.app import create_app as create_meet

ROOT = Path(__file__).resolve().parents[1]
MINUTES = ROOT / "blocks/ui-meet/src/vai_ui_meet/static/minutes.html"
DEMO = ROOT / "blocks/core-gw/src/vai_core_gw/static/demo.html"


class _Bus:
    async def close(self) -> None:
        return None


def _meet(gateway: httpx.AsyncClient | None = None) -> FastAPI:
    return create_meet(
        bus=_Bus(),  # type: ignore[arg-type]
        summary_client=httpx.AsyncClient(base_url="http://summary"),
        diarization_client=httpx.AsyncClient(base_url="http://dia"),
        gateway_client=gateway,
    )


def test_회의를_열_수_있다고_알린다() -> None:
    """화면은 이걸 보고 시작 버튼을 그릴지 정한다. 눌리는데 아무 일도 안
    나는 버튼보다 없는 편이 낫다."""
    with TestClient(_meet()) as client:
        response = client.get("/v1/meet/capabilities")

    assert response.status_code == 200
    assert response.json() == {"mic": True}


def test_회의_시작이_게이트웨이를_대신_부른다() -> None:
    """브라우저가 게이트웨이를 직접 부르면 포트가 둘로 늘고, 역방향 프록시
    뒤에서 깨진다. 화면은 UI-MEET 만 봐야 한다."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(
            201,
            json={"session": {"session_id": "sess_1", "profile": "meeting"}, "ws_token": "tok"},
        )

    gateway = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://core-gw:8080"
    )
    with TestClient(_meet(gateway)) as client:
        response = client.post("/v1/meet/sessions")

    assert response.status_code == 200, response.text
    assert response.json() == {"session_id": "sess_1", "ws_token": "tok"}
    assert seen["path"] == "/v1/sessions"
    assert "meeting" in str(seen["body"]), "회의록 프로파일로 열어야 한다"


def test_게이트웨이가_말한_이유를_그대로_올린다() -> None:
    """여기서 "회의를 시작할 수 없다"로 뭉개면, 끝난 세션이라 막힌 것인지
    인증이 없어 막힌 것인지 화면에서 구분할 수 없다."""
    gateway = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(409, json={"detail": "이미 끝난 세션이다 (상태: closed)"})
        ),
        base_url="http://core-gw:8080",
    )
    with TestClient(_meet(gateway)) as client:
        response = client.post("/v1/meet/sessions/sess_1/join")

    assert response.status_code == 409
    assert "끝난 세션" in response.json()["detail"]


def test_게이트웨이가_죽어도_이유를_말한다() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    gateway = httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://core-gw:8080"
    )
    with TestClient(_meet(gateway)) as client:
        response = client.post("/v1/meet/sessions")

    assert response.status_code == 503
    assert "CORE-GW" in response.json()["detail"], "무엇이 안 떠 있는지 말해야 한다"


# ── 화면 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("screen", "path"),
    [("회의록", MINUTES), ("데모", DEMO)],
)
def test_화면이_부르는_경로가_서버에_있다(screen: str, path: Path) -> None:
    """경로 오타는 예외가 아니라 **아무 일도 안 일어나는 버튼**으로 나타난다."""
    import re

    html = path.read_text("utf-8")
    app = _meet() if screen == "회의록" else create_gateway()
    routes = {re.sub(r"\{[^}]+\}", "{p}", r.path) for r in app.routes if hasattr(r, "path")}

    called = {
        re.sub(r"\$\{[^}]+\}", "{p}", raw) for raw in re.findall(r'["`](/v1/[^"`\s?]*)', html)
    }
    assert called, f"{screen}: 화면이 어떤 API도 부르지 않는다 — 정규식이 낡았다"

    for endpoint in called:
        assert re.sub(r"\{[^}]+\}", "{p}", endpoint) in routes, (
            f"{screen} 화면이 없는 경로를 부른다: {endpoint}"
        )


def test_회의록_화면에_시작_버튼이_있다() -> None:
    html = MINUTES.read_text("utf-8")

    assert 'id="start"' in html, "회의를 시작할 방법이 화면에 없다"
    assert "getUserMedia" in html, "마이크를 잡지 않는다"
    assert "close_session" in html, "종료를 안 보내면 요약이 영영 안 돈다"


@pytest.mark.parametrize("path", [MINUTES, DEMO], ids=["회의록", "데모"])
def test_참여_링크를_보여_준다(path: Path) -> None:
    """여럿이 붙는 길이 화면에 안 보이면 없는 기능이나 마찬가지다."""
    html = path.read_text("utf-8")

    assert 'id="invite"' in html
    assert 'searchParams.set("session"' in html, "참여 주소를 만들어 보여 줘야 한다"
    assert '.get("session")' in html, "참여 주소로 들어왔을 때 그 회의에 붙어야 한다"
