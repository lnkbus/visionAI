"""화면이 **실제로 있는 API**를 부르는가.

운영 화면의 가장 흔한 고장은 예외가 아니라 침묵이다 — 경로를 한 글자 틀리면
탭은 열리는데 표가 비고, 사람은 "데이터가 없나 보다"라고 읽는다. 그 상태는
시연 중에 발견되고, 그때는 고칠 시간이 없다.

그래서 정적으로 대조한다: 화면이 부르는 경로가 서버에 실제로 있는가, 탭
버튼마다 대응하는 화면이 있는가, 버튼이 부르는 함수가 정의돼 있는가.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLES = {
    "CORE-ADM": ROOT / "blocks/core-adm/src/vai_core_adm/static/console.html",
    "SCN-STUDIO": ROOT / "blocks/scn-studio/src/vai_scn_studio/static/console.html",
}

_TAB_BUTTON = re.compile(r'data-tab="([\w-]+)"')
_SECTION = re.compile(r'<section id="tab-([\w-]+)"')
_ELEMENT_ID = re.compile(r'\sid="([\w-]+)"')
_GETTER = re.compile(r'\$\("([\w-]+)"\)')


@pytest.mark.parametrize("name", sorted(CONSOLES))
def test_탭_버튼마다_화면이_있다(name: str) -> None:
    """버튼은 있는데 화면이 없으면 아무 반응도 없는 버튼이 된다."""
    html = CONSOLES[name].read_text("utf-8")
    buttons = set(_TAB_BUTTON.findall(html))
    sections = set(_SECTION.findall(html))

    assert buttons, f"{name}: 탭 버튼을 못 찾았다"
    assert buttons == sections, f"{name}: 짝이 없는 탭 {buttons ^ sections}"


@pytest.mark.parametrize("name", sorted(CONSOLES))
def test_스크립트가_없는_요소를_찾지_않는다(name: str) -> None:
    """``$("...")``가 null을 돌려주면 그 지점부터 화면이 조용히 멈춘다."""
    html = CONSOLES[name].read_text("utf-8")
    declared = set(_ELEMENT_ID.findall(html))
    used = set(_GETTER.findall(html))

    assert used <= declared, f"{name}: 정의되지 않은 요소 {sorted(used - declared)}"


def _admin_routes() -> set[str]:
    from vai_core_adm.app import create_app

    app = create_app(client=_NullClient(), bus=_NullBus())
    return {route.path for route in app.routes if hasattr(route, "path")}


class _NullClient:
    async def aclose(self) -> None:  # pragma: no cover - 기동만 시킨다
        return None


class _NullBus:
    async def close(self) -> None:  # pragma: no cover
        return None


def test_운영_콘솔이_부르는_경로가_전부_존재한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """경로 오타는 예외가 아니라 빈 화면으로 나타난다 — 가장 늦게 발견된다."""
    monkeypatch.setenv("VAI_ADM_REQUIRE_AUTH", "false")
    html = CONSOLES["CORE-ADM"].read_text("utf-8")
    routes = _admin_routes()

    # 템플릿 리터럴의 ${...} 를 FastAPI 경로 매개변수로 되돌린다.
    called = set()
    for raw in re.findall(r'["`](/v1/admin[^"`\s]*)["`]', html):
        called.add(re.sub(r"\$\{[^}]+\}", "{p}", raw))

    assert called, "콘솔이 어떤 API도 부르지 않는다 — 정규식이 낡았다"
    for path in called:
        normalized = re.sub(r"\{[^}]+\}", "{p}", path)
        matches = {re.sub(r"\{[^}]+\}", "{p}", route) for route in routes}
        assert normalized in matches, f"서버에 없는 경로: {path}"


def test_저작_콘솔이_부르는_경로가_전부_존재한다() -> None:
    from vai_scn_studio.app import create_app

    app = create_app()
    routes = {re.sub(r"\{[^}]+\}", "{p}", r.path) for r in app.routes if hasattr(r, "path")}
    html = CONSOLES["SCN-STUDIO"].read_text("utf-8")

    called = set()
    for raw in re.findall(r'["`](/internal/v1[^"`\s?]*)', html):
        called.add(re.sub(r"\$\{[^}]+\}", "{p}", raw))

    assert called, "저작 콘솔이 어떤 API도 부르지 않는다 — 정규식이 낡았다"
    for path in called:
        assert re.sub(r"\{[^}]+\}", "{p}", path) in routes, f"서버에 없는 경로: {path}"


def test_생애주기_단계_이름이_계약과_같다() -> None:
    """화면이 제 이름을 쓰면 승격 요청이 조용히 422로 떨어진다."""
    from vai_contracts.dialog import STAGE_ORDER

    html = CONSOLES["SCN-STUDIO"].read_text("utf-8")
    rendered = re.search(r"const STAGES = \[(.*?)\];", html, re.S)

    assert rendered is not None, "STAGES 정의를 못 찾았다"
    keys = re.findall(r'\["(\w+)"', rendered.group(1))
    assert keys == [stage.value for stage in STAGE_ORDER]


def test_통계_화면이_평균의_평균을_쓰지_않는다() -> None:
    """일별 평균 칸은 그 날의 합계와 건수로 직접 낸다.

    화면에서 평균을 다시 평균하면 통화가 많았던 날과 적었던 날이 같은 무게가
    되고, 그 값은 조용히 틀린다.
    """
    html = CONSOLES["CORE-ADM"].read_text("utf-8")

    assert "d.confidence_sum / d.confidence_n" in html


def test_이력_화면이_마스킹본임을_명시한다() -> None:
    """무엇이 저장되는지 화면이 말하지 않으면 운영자가 원문으로 오해한다."""
    html = CONSOLES["CORE-ADM"].read_text("utf-8")

    assert "마스킹본" in html
    assert "감사에 기록" in html
