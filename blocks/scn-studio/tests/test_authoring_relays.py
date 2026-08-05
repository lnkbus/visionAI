"""저작 콘솔이 중계하는 두 화면 — 대화 시뮬레이터·읽기 미리듣기.

둘 다 **다른 블록의 API를 그대로 중계한다.** 콘솔이 자체 구현을 갖지 않는
이유는 시험 경로와 운영 경로가 갈리면 안 되기 때문이다 — 갈리는 순간
"시뮬레이터에서는 되는데 실제로는 안 된다"가 가능해지고, 그 사실은 배포 후에
드러난다.

그래서 여기서 보는 것은 "결과가 맞는가"가 아니라 **"진짜 그 블록에 갔는가"**다.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from vai_scn_studio.app import create_app

SCENARIO = {
    "tenant_id": "demo",
    "scenario_id": "s1",
    "entry_node": "greet",
    "nodes": [{"node_id": "greet", "kind": "end", "prompt": "안녕하세요"}],
}


def _client_to(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://block")


@pytest.fixture
def studio():  # type: ignore[no-untyped-def]
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/dry-run"):
            return httpx.Response(200, json={"problems": [], "turns": [], "finished": True})
        return httpx.Response(200, json={"original": "2,000원", "normalized": "이천 원"})

    app = create_app()
    with TestClient(app) as client:
        # 기동 후에 갈아 끼운다 — lifespan 이 실제 클라이언트를 만든 뒤다.
        app.state.bot = _client_to(handler)
        app.state.tts = _client_to(handler)
        yield client, seen


def test_시뮬레이터가_대화_엔진에_그대로_간다(studio) -> None:  # type: ignore[no-untyped-def]
    client, seen = studio
    response = client.post(
        "/internal/v1/simulate", json={"scenario": SCENARIO, "utterances": ["가"]}
    )

    assert response.status_code == 200
    path, body = seen[-1]
    assert path == "/internal/v1/scenarios/dry-run", "다른 곳으로 갔다"
    assert body["scenario"]["scenario_id"] == "s1", "시나리오가 변형됐다"
    assert body["utterances"] == ["가"], "발화가 변형됐다"


def test_미리듣기가_tts에_그대로_간다(studio) -> None:  # type: ignore[no-untyped-def]
    client, seen = studio
    response = client.post("/internal/v1/tts/preview", json={"text": "2,000원"})

    assert response.status_code == 200
    path, body = seen[-1]
    assert path == "/internal/v1/tts/preview"
    assert body["text"] == "2,000원"
    assert response.json()["normalized"] == "이천 원"


def test_블록이_죽어_있으면_이유를_말한다() -> None:
    """'Internal Server Error' 만 나오면 운영자가 할 수 있는 일이 없다."""

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패")

    app = create_app()
    with TestClient(app) as client:
        app.state.bot = _client_to(dead)
        response = client.post(
            "/internal/v1/simulate", json={"scenario": SCENARIO, "utterances": []}
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "dry-run" in detail, "어느 경로가 죽었는지 알려주지 않는다"
    assert "연결할 수 없다" in detail


def test_블록이_거절하면_그_이유를_그대로_넘긴다() -> None:
    """422 를 500 으로 바꾸면 시나리오가 틀렸다는 사실이 사라진다."""

    def rejects(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "entry_node 가 없다"})

    app = create_app()
    with TestClient(app) as client:
        app.state.bot = _client_to(rejects)
        response = client.post(
            "/internal/v1/simulate", json={"scenario": SCENARIO, "utterances": []}
        )

    assert response.status_code == 422
    assert "entry_node" in response.json()["detail"]
