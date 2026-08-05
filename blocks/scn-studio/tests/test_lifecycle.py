"""시나리오 생애주기 · 반출입 · 학습 도구.

이 기능이 지키려는 것은 하나다: **검증되지 않은 시나리오가 운영에 올라가지
않는다.** 막다른 골목이 있는 흐름이 배포되면 고객이 통화 안에 갇히고, 그
사실은 통화가 끊긴 뒤에야 드러난다.

그래서 여기서 가장 중요한 시험은 "정상 경로가 되는가"가 아니라 **"지름길이
막혀 있는가"** 다.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from vai_contracts.dialog import Scenario, ScenarioRevision, ScenarioStage
from vai_scn_studio.app import create_app
from vai_scn_studio.lifecycle import (
    InMemoryScenarioStore,
    StageError,
    blank_scenario,
    check_promotion,
    clone_flow,
    export_bundle,
    import_bundle,
    intents_from_csv,
    intents_to_csv,
    touch_draft,
)
from vai_scn_studio.store import InMemoryAuthoringStore

TENANT = "demo"


def scenario() -> Scenario:
    return Scenario.model_validate(
        {
            "scenario_id": "s1",
            "tenant_id": TENANT,
            "entry_node": "start",
            "fallback_node": "handoff",
            "nodes": [
                {
                    "node_id": "start",
                    "kind": "ask",
                    "prompt": "무엇을 도와드릴까요?",
                    "slot": "topic",
                    "intents": [
                        {
                            "name": "card",
                            "patterns": ["카드"],
                            "examples": ["카드 재발급이요"],
                            "next_node": "card",
                        }
                    ],
                },
                {"node_id": "card", "kind": "say", "prompt": "카드 안내", "next_node": "handoff"},
                {"node_id": "handoff", "kind": "handoff", "prompt": "연결합니다"},
            ],
        }
    )


# ── 단계 규칙 ────────────────────────────────────────────────────────────────


def test_한_칸씩만_올라간다() -> None:
    """DRAFT에서 곧장 LIVE로 가는 길이 있으면 결국 모두가 그 길로 간다."""
    with pytest.raises(StageError, match="건너뛸 수 없다"):
        check_promotion(ScenarioStage.DRAFT, ScenarioStage.LIVE)

    check_promotion(ScenarioStage.DRAFT, ScenarioStage.BUILT)


def test_뒤로는_승격할_수_없다() -> None:
    with pytest.raises(StageError):
        check_promotion(ScenarioStage.LIVE, ScenarioStage.BUILT)


def test_편집하면_초안으로_내려간다() -> None:
    """검증을 통과한 뒤 한 글자만 고쳐도 그것은 검증된 물건이 아니다."""
    revision = ScenarioRevision(scenario=scenario(), stage=ScenarioStage.STAGING)

    touch_draft(revision, note="문구 수정")

    assert revision.stage is ScenarioStage.DRAFT
    assert revision.note == "문구 수정"


def test_빈_시나리오도_사람_연결에_닿는다() -> None:
    """빈 시나리오가 곧바로 검증 실패로 나오면 저작자는 도구가 고장 났다고 생각한다."""
    from vai_bot_voice.engine import validate

    assert validate(blank_scenario(TENANT)) == []


# ── 반출입 ───────────────────────────────────────────────────────────────────


def test_반출본은_단계를_싣지_않는다() -> None:
    bundle = export_bundle(ScenarioRevision(scenario=scenario(), stage=ScenarioStage.LIVE))

    assert "stage" not in bundle
    assert bundle["format"] == "visionai.scenario/v1"


def test_반입은_받는_쪽_테넌트로_덮어쓴다() -> None:
    """꾸러미 안 테넌트를 믿으면 파일 하나로 남의 영역에 쓰기가 일어난다."""
    bundle = export_bundle(ScenarioRevision(scenario=scenario()))

    restored = import_bundle(bundle, tenant_id="other")

    assert restored.tenant_id == "other"
    assert not restored.published, "반입본이 배포 상태로 들어오면 안 된다"


def test_모르는_형식은_거부한다() -> None:
    with pytest.raises(ValueError, match="모르는 형식"):
        import_bundle({"format": "someone-else/v9", "scenario": {}}, tenant_id=TENANT)


def test_시나리오가_없는_꾸러미는_이유를_말한다() -> None:
    with pytest.raises(ValueError, match="scenario 항목"):
        import_bundle({"format": "visionai.scenario/v1"}, tenant_id=TENANT)


# ── 흐름 복제 ────────────────────────────────────────────────────────────────


def test_복제는_안쪽_연결만_새_이름으로_바꾼다() -> None:
    cloned = clone_flow(scenario(), ["start", "card"], suffix="-vip")

    ids = {node.node_id for node in cloned.nodes}
    assert {"start-vip", "card-vip"} <= ids

    copy = next(n for n in cloned.nodes if n.node_id == "start-vip")
    assert copy.intents[0].next_node == "card-vip", "안쪽 연결은 복제본을 가리켜야 한다"

    card = next(n for n in cloned.nodes if n.node_id == "card-vip")
    assert card.next_node == "handoff", "바깥 연결은 그대로 둔다 — 종료 지점을 공유한다"


def test_복제가_기존_노드와_충돌하면_거부한다() -> None:
    """같은 접미사로 두 번 복제하면 두 번째는 이미 있는 ID를 만든다."""
    once = clone_flow(scenario(), ["start"], suffix="-vip")

    with pytest.raises(ValueError, match="이미 있는 노드"):
        clone_flow(once, ["start"], suffix="-vip")


def test_접미사가_없으면_복제하지_않는다() -> None:
    with pytest.raises(ValueError, match="접미사"):
        clone_flow(scenario(), ["start"], suffix="")


# ── 학습 도구 ────────────────────────────────────────────────────────────────


def test_표에_bom이_붙는다() -> None:
    """없으면 엑셀이 한글을 깨서 열고, 한 번 깨져 본 사람은 그 도구를 안 쓴다."""
    assert intents_to_csv(scenario()).startswith("﻿")


def test_인텐트가_없는_노드도_표에_남는다() -> None:
    """여기가 비면 '빠뜨린 것'과 '원래 없는 것'이 같아 보인다."""
    rows = intents_to_csv(scenario()).splitlines()

    assert any(row.startswith("handoff,") for row in rows)


def test_표를_왕복해도_인텐트가_보존된다() -> None:
    original = scenario()

    restored = intents_from_csv(intents_to_csv(original), scenario=original)

    start = next(n for n in restored.nodes if n.node_id == "start")
    assert start.intents[0].name == "card"
    assert start.intents[0].patterns == ["카드"]
    assert start.intents[0].examples == ["카드 재발급이요"]


def test_표가_노드를_새로_만들지_않는다() -> None:
    """오타 하나로 아무 데도 연결되지 않은 노드가 생기면, 그 노드는 검증만
    통과하고 아무 일도 하지 않는다."""
    csv_text = "node_id,intent,next_node,patterns,examples\nstrat,card,card,카드,\n"

    with pytest.raises(ValueError, match="시나리오에 없는 노드"):
        intents_from_csv(csv_text, scenario=scenario())


def test_양식이_아니면_이유를_말한다() -> None:
    with pytest.raises(ValueError, match="열이 없다"):
        intents_from_csv("아무거나\n1\n", scenario=scenario())


# ── API ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def studio():  # type: ignore[no-untyped-def]
    """BOT-VOICE 를 흉내 낸다. 검증·배포가 **정말 그쪽으로 가는지** 본다."""
    seen: list[tuple[str, str, dict]] = []
    problems: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json={"problems": list(problems)})
        if request.method == "PUT":
            body["version"] = body.get("version", 1)
            body["published"] = True
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={})

    # 저장소를 주입한다. 주입하지 않으면 개발 장비의 Redis 를 그대로 쓰게 되고,
    # 그러면 이 시험은 다른 시험이 남긴 이력에 따라 결과가 바뀐다.
    app = create_app(store=InMemoryAuthoringStore(), scenarios=InMemoryScenarioStore())
    with TestClient(app) as client:
        app.state.bot = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://bot"
        )
        yield client, seen, problems


def _save(client: TestClient) -> None:
    response = client.put(
        f"/internal/v1/scenarios/{TENANT}", json=scenario().model_dump(mode="json")
    )
    assert response.status_code == 200


def test_저장하면_초안_단계다(studio) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = studio
    _save(client)

    assert client.get(f"/internal/v1/scenarios/{TENANT}").json()["stage"] == "draft"


def test_운영으로_바로_올릴_수_없다(studio) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = studio
    _save(client)

    response = client.post(f"/internal/v1/scenarios/{TENANT}/promote", params={"stage": "live"})

    assert response.status_code == 409
    assert "건너뛸 수 없다" in response.json()["detail"]


def test_검증은_봇_블록이_한다(studio) -> None:  # type: ignore[no-untyped-def]
    """저작 콘솔이 자체 검증을 두면 '여기선 통과인데 배포에서 거부'가 생긴다."""
    client, seen, _ = studio
    _save(client)

    response = client.post(f"/internal/v1/scenarios/{TENANT}/promote", params={"stage": "built"})

    assert response.status_code == 200
    assert response.json()["stage"] == "built"
    assert any(path.endswith("/scenarios/validate") for _, path, _ in seen)


def test_검증에_걸리면_승격하지_않고_문제를_남긴다(studio) -> None:  # type: ignore[no-untyped-def]
    client, _, problems = studio
    problems.append("handoff 에 도달할 수 없다")
    _save(client)

    response = client.post(f"/internal/v1/scenarios/{TENANT}/promote", params={"stage": "built"})

    assert response.status_code == 400
    assert client.get(f"/internal/v1/scenarios/{TENANT}").json()["stage"] == "draft"
    assert client.get(f"/internal/v1/scenarios/{TENANT}").json()["problems"] == [
        "handoff 에 도달할 수 없다"
    ]


def test_운영_승격은_실제로_배포한다(studio) -> None:  # type: ignore[no-untyped-def]
    """단계 표시만 바꾸고 배포하지 않으면 화면과 현실이 갈라진다."""
    client, seen, _ = studio
    _save(client)
    for stage in ("built", "staging", "live"):
        response = client.post(f"/internal/v1/scenarios/{TENANT}/promote", params={"stage": stage})
        assert response.status_code == 200, response.text

    assert response.json()["stage"] == "live"
    assert any(
        method == "PUT" and path == f"/internal/v1/scenarios/{TENANT}" for method, path, _ in seen
    ), "BOT-VOICE 에 실제 배포가 가지 않았다"


def test_배포하면_되돌릴_판이_남는다(studio) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = studio
    _save(client)
    for stage in ("built", "staging", "live"):
        client.post(f"/internal/v1/scenarios/{TENANT}/promote", params={"stage": stage})

    history = client.get(f"/internal/v1/scenarios/{TENANT}/history").json()
    assert len(history) == 1

    restored = client.post(
        f"/internal/v1/scenarios/{TENANT}/restore",
        params={"version": history[0]["scenario"]["version"]},
    )

    assert restored.status_code == 200
    assert restored.json()["stage"] == "draft", "되돌리기도 배포이므로 같은 검증을 거친다"


def test_반출한_것을_그대로_반입할_수_있다(studio) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = studio
    _save(client)
    bundle = client.get(f"/internal/v1/scenarios/{TENANT}/export").json()

    response = client.post(f"/internal/v1/scenarios/{TENANT}/import", json=bundle)

    assert response.status_code == 200
    assert response.json()["scenario"]["scenario_id"] == "s1"


def test_망가진_꾸러미는_400을_준다(studio) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = studio

    response = client.post(f"/internal/v1/scenarios/{TENANT}/import", json={"scenario": "글자"})

    assert response.status_code == 400


def test_인텐트_표를_내려받고_올릴_수_있다(studio) -> None:  # type: ignore[no-untyped-def]
    client, _, _ = studio
    _save(client)

    downloaded = client.get(f"/internal/v1/scenarios/{TENANT}/intents.csv")
    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]

    edited = downloaded.text.replace("카드 재발급이요", "카드 재발급이요|카드 잃어버렸어요")
    response = client.post(f"/internal/v1/scenarios/{TENANT}/intents.csv", json={"csv": edited})

    assert response.status_code == 200
    start = next(n for n in response.json()["scenario"]["nodes"] if n["node_id"] == "start")
    assert start["intents"][0]["examples"] == ["카드 재발급이요", "카드 잃어버렸어요"]


# ── TTS 읽기 사전 ────────────────────────────────────────────────────────────


def test_읽기_사전이_배포_채널로_나간다(studio) -> None:  # type: ignore[no-untyped-def]
    """실시간 합성 블록이 저작 도구를 직접 부르면, 콘솔이 죽는 날 봇도
    함께 이상해진다. 사전은 배포 채널로 흘러야 한다."""
    from vai_common.config_store import ConfigKind
    from vai_contracts.authoring import TtsLexicon

    client, _, _ = studio
    response = client.put(
        f"/internal/v1/tts-lexicon/{TENANT}",
        json={
            "tenant_id": TENANT,
            "readings": [{"surface": "무배당행복플러스", "reading": "무배당 행복 플러스"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["version"] == 1

    async def loaded():  # type: ignore[no-untyped-def]
        return await client.app.state.configs.load(ConfigKind.TTS_LEXICON, TENANT, TtsLexicon)

    published = asyncio.run(loaded())
    assert published is not None
    assert published.readings[0].reading == "무배당 행복 플러스"


def test_저장할_때마다_버전이_오른다(studio) -> None:  # type: ignore[no-untyped-def]
    """버전이 그대로면 합성 쪽 캐시가 새 사전을 다시 만들지 않는다."""
    client, _, _ = studio
    body = {"tenant_id": TENANT, "readings": [{"surface": "가", "reading": "갸"}]}

    client.put(f"/internal/v1/tts-lexicon/{TENANT}", json=body)
    second = client.put(f"/internal/v1/tts-lexicon/{TENANT}", json=body)

    assert second.json()["version"] == 2


def test_사전이_없으면_빈_사전을_준다(studio) -> None:  # type: ignore[no-untyped-def]
    """404 를 주면 저작 화면이 첫 진입에서 오류만 보여 준다."""
    client, _, _ = studio

    response = client.get("/internal/v1/tts-lexicon/never-seen")

    assert response.status_code == 200
    assert response.json()["readings"] == []
