"""포트만 알고 브라우저를 연 사람이 화면을 찾을 수 있는가.

맥 데모에서 실제로 일어난 일이다. 안내표의 주소를 놓치고 포트만 쳤더니
이것이 나왔다:

    {"detail":"Not Found"}

틀린 말은 아니다. 회의록 화면은 `/minutes` 에 멀쩡히 떠 있었다. 하지만 그
404 는 **화면이 있다는 사실 자체를 숨긴다.** 포트를 아는 사람이 가장 먼저
하는 일이 `localhost:<포트>` 를 여는 것이고, 거기서 막혔다.

그래서 두 가지를 정한다.

- 화면이 있는 블록은 `/` 에서 그 화면으로 보낸다.
- 화면이 없는 블록은 `/` 에서 **자기가 무엇인지와 어디를 볼지** 말한다.
  API 블록이라는 사실을 알려 주는 것만으로도 다음 행동이 정해진다.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vai_common.service import create_block_app, set_landing

# 화면을 가진 블록과 그 첫 화면. compose 의 포트도 함께 적는다 —
# 사람이 실제로 여는 것은 포트이기 때문이다.
SCREENS = [
    ("vai_core_gw.app", 8080, "/demo"),
    ("vai_scn_studio.app", 8090, "/console"),
    ("vai_ui_agent.app", 8091, "/workspace"),
    ("vai_ui_meet.app", 8094, "/minutes"),
    ("vai_core_adm.app", 8097, "/console"),
]


@pytest.fixture(autouse=True)
def _demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """compose 데모와 같은 구성. CORE-ADM 은 기본 비밀키로는 아예 안 뜬다."""
    monkeypatch.setenv("VAI_ADM_REQUIRE_AUTH", "false")


def _app(module_name: str) -> FastAPI:
    from importlib import import_module

    app = import_module(module_name).create_app()
    assert isinstance(app, FastAPI)
    return app


@pytest.mark.parametrize(("module_name", "port", "screen"), SCREENS)
def test_포트만_열어도_화면으로_간다(module_name: str, port: int, screen: str) -> None:
    # lifespan 을 안 띄운다. 여기서 보는 것은 경로뿐이고, 블록에 따라 기동에
    # Redis 가 필요하다 — 그것까지 끌어오면 이 시험이 인프라 시험이 된다.
    response = TestClient(_app(module_name)).get("/", follow_redirects=False)

    assert response.status_code in {307, 308, 302}, (
        f"localhost:{port} 를 열면 {response.status_code} 다 — 화면이 있는데 못 찾는다"
    )
    assert response.headers["location"] == screen


@pytest.mark.parametrize(("module_name", "port", "screen"), SCREENS)
def test_따라가면_실제로_화면이_나온다(module_name: str, port: int, screen: str) -> None:
    """보내는 곳이 또 404 면 한 번 더 헤매게 된다."""
    response = TestClient(_app(module_name)).get("/", follow_redirects=True)

    assert response.status_code == 200, f"{screen} 이 {response.status_code} 다"
    assert "<" in response.text[:200], "화면이 아니라 다른 것이 왔다"


def test_화면이_없는_블록은_자기가_무엇인지_말한다() -> None:
    app = create_block_app(block_id="AUD-VAD", title="발화 구간 검출")

    response = TestClient(app).get("/")

    assert response.status_code == 200, "빈 404 로 돌아갔다"
    body = response.text
    assert "AUD-VAD" in body, "어느 블록인지 말해야 한다"
    assert "/healthz" in body and "/docs" in body, "다음에 볼 곳을 적어야 한다"


def test_안내_화면은_바깥으로_나가지_않는다() -> None:
    """폐쇄망에서 CDN 을 부르면 그냥 빈 화면이 된다. 안내하려고 만든 것이
    안내를 못 하는 자리다."""
    app = create_block_app(block_id="AUD-VAD", title="발화 구간 검출")

    body = TestClient(app).get("/").text

    assert "http://" not in body and "https://" not in body, f"바깥 자원을 부른다: {body}"


def test_첫_화면을_정하면_안내가_밀려난다() -> None:
    """FastAPI 는 먼저 걸린 경로가 이긴다. 기본 안내를 안 걷어내면 새로 정한
    첫 화면이 영원히 안 불린다 — 조용히, 아무 오류 없이."""
    app = create_block_app(block_id="UI-MEET", title="회의록")
    set_landing(app, "/minutes")

    roots = [route for route in app.router.routes if getattr(route, "path", "") == "/"]

    assert len(roots) == 1, f"/ 가 {len(roots)} 개다 — 뒤엣것은 안 불린다"
