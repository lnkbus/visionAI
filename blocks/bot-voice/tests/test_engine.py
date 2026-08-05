"""음성봇 대화 엔진.

음성봇에서 가장 자주 깨지는 것은 모델이 아니라 **막다른 골목**이다.
못 알아들어 되묻고, 또 못 알아들어 되묻고, 고객은 결국 끊는다.
그 경계를 여기서 고정한다.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from vai_bot_voice.app import create_app
from vai_bot_voice.engine import DialogEngine, match_intent, validate
from vai_bot_voice.store import InMemoryDialogStore
from vai_bot_voice.worker import BotWorker
from vai_common.bus import InMemoryEventBus
from vai_contracts.dialog import DialogNode, Intent, NodeKind, Scenario
from vai_contracts.events import FilterResult
from vai_contracts.session import ChannelRole
from vai_contracts.topics import Topic


def _scenario(**overrides: object) -> Scenario:
    """카드 재발급 시나리오 — 실제 AICC에서 가장 흔한 흐름."""
    base = Scenario(
        scenario_id="card-reissue",
        tenant_id="acme",
        entry_node="start",
        fallback_node="agent",
        nodes=[
            DialogNode(
                node_id="start",
                kind=NodeKind.SAY,
                prompt="안녕하세요. 카드 상담입니다.",
                next_node="ask_purpose",
            ),
            DialogNode(
                node_id="ask_purpose",
                kind=NodeKind.ASK,
                prompt="어떤 도움이 필요하신가요?",
                reprompt="재발급, 분실신고 중에 말씀해 주세요.",
                slot="purpose",
                max_retries=2,
                intents=[
                    Intent(name="reissue", patterns=["재발급", "다시.*발급"], next_node="confirm"),
                    Intent(name="lost", patterns=["분실", "잃어"], next_node="agent"),
                ],
            ),
            DialogNode(
                node_id="confirm",
                kind=NodeKind.SAY,
                prompt="재발급 접수해 드리겠습니다.",
                next_node="done",
            ),
            DialogNode(node_id="agent", kind=NodeKind.HANDOFF, prompt="상담원을 연결합니다."),
            DialogNode(node_id="done", kind=NodeKind.END, prompt="이용해 주셔서 감사합니다."),
        ],
    )
    return base.model_copy(update=dict(overrides))


# --- 정상 흐름 -----------------------------------------------------------


def test_시작하면_인사하고_묻는다() -> None:
    result = DialogEngine(_scenario()).start("s1", "acme")
    assert "안녕하세요" in result.text
    assert "어떤 도움이" in result.text  # say → ask가 한 번에 이어진다
    assert result.state is not None and result.state.current_node == "ask_purpose"


def test_인텐트를_맞추면_다음으로_간다() -> None:
    engine = DialogEngine(_scenario())
    state = engine.start("s1", "acme").state
    assert state is not None

    step = engine.reply(state, "카드 재발급 받고 싶어요")
    assert "재발급 접수" in step.text
    assert step.state is not None and step.state.finished


def test_슬롯에_고객_발화가_담긴다() -> None:
    engine = DialogEngine(_scenario())
    state = engine.start("s1", "acme").state
    assert state is not None
    step = engine.reply(state, "재발급이요")
    assert step.state is not None and step.state.slots["purpose"] == "재발급이요"


def test_먼저_선언된_인텐트가_이긴다() -> None:
    """저작자가 순서로 우선순위를 표현할 수 있어야 한다."""
    node = _scenario().node("ask_purpose")
    assert node is not None
    matched = match_intent(node, "재발급 아니고 분실이요")
    assert matched is not None and matched.name == "reissue"


# --- 막다른 골목 ---------------------------------------------------------


def test_되묻는_말은_처음과_다르다() -> None:
    """같은 문장을 반복하면 고객은 봇이 고장 났다고 여기고 말을 멈춘다."""
    engine = DialogEngine(_scenario())
    state = engine.start("s1", "acme").state
    assert state is not None

    step = engine.reply(state, "음... 글쎄요")
    assert "재발급, 분실신고 중에" in step.text
    assert "어떤 도움이" not in step.text


def test_재질문_상한을_넘으면_사람에게_넘긴다() -> None:
    """무한 재질문이 이 영역 최악의 경험이다."""
    engine = DialogEngine(_scenario())
    state = engine.start("s1", "acme").state
    assert state is not None

    for _ in range(2):
        state = engine.reply(state, "어어").state
        assert state is not None and not state.handed_off

    step = engine.reply(state, "여전히 모르겠어요")
    assert step.state is not None and step.state.handed_off
    assert "상담원" in step.text


def test_인텐트를_맞추면_재시도_횟수가_초기화된다() -> None:
    """한 번 못 알아들었다고 그 통화 내내 여유가 줄면 안 된다."""
    engine = DialogEngine(_scenario())
    state = engine.start("s1", "acme").state
    assert state is not None

    state = engine.reply(state, "어어").state
    assert state is not None and state.retries == 1
    state = engine.reply(state, "재발급").state
    assert state is not None and state.retries == 0


def test_무응답도_못_알아들음으로_센다() -> None:
    """침묵이 계속되는데 계속 기다리면 통화가 멈춘 것처럼 들린다."""
    engine = DialogEngine(_scenario())
    state = engine.start("s1", "acme").state
    assert state is not None

    for _ in range(3):
        step = engine.timeout(state)
        state = step.state
        assert state is not None
    assert state.handed_off


def test_폴백이_없어도_마지막_안내는_한다() -> None:
    """아무 말 없이 끊기는 것보다는 낫다."""
    broken = _scenario(
        fallback_node="",
        nodes=[
            DialogNode(
                node_id="start",
                kind=NodeKind.ASK,
                prompt="말씀하세요",
                reprompt="다시 말씀해 주세요",
                max_retries=0,
                intents=[Intent(name="x", patterns=["없는패턴"], next_node="start")],
            )
        ],
    )
    engine = DialogEngine(broken)
    state = engine.start("s1", "acme").state
    assert state is not None

    step = engine.reply(state, "아무말")
    assert step.state is not None and step.state.handed_off
    assert "상담원" in step.text


def test_순환하는_시나리오는_사람에게_넘긴다() -> None:
    """고객은 봇이 혼자 끝없이 말하는 것을 듣게 된다."""
    looping = Scenario(
        scenario_id="loop",
        tenant_id="acme",
        entry_node="a",
        fallback_node="agent",
        nodes=[
            DialogNode(node_id="a", kind=NodeKind.SAY, prompt="가", next_node="b"),
            DialogNode(node_id="b", kind=NodeKind.SAY, prompt="나", next_node="a"),
            DialogNode(node_id="agent", kind=NodeKind.HANDOFF, prompt="상담원 연결"),
        ],
    )
    result = DialogEngine(looping).start("s1", "acme")
    assert result.state is not None and result.state.handed_off


# --- 배포 전 검사 --------------------------------------------------------


def test_정상_시나리오는_문제가_없다() -> None:
    assert validate(_scenario()) == []


def test_사람_연결_경로가_없으면_거부한다() -> None:
    """빠져나갈 곳 없는 봇은 고객을 가둔다. 그것 하나로 브랜드가 상한다."""
    no_exit = Scenario(
        scenario_id="trap",
        tenant_id="acme",
        entry_node="a",
        nodes=[DialogNode(node_id="a", kind=NodeKind.END, prompt="끝")],
    )
    assert any("상담원" in p for p in validate(no_exit))


def test_도달할_수_없는_사람_연결은_없는_것과_같다() -> None:
    unreachable = Scenario(
        scenario_id="orphan",
        tenant_id="acme",
        entry_node="a",
        nodes=[
            DialogNode(node_id="a", kind=NodeKind.END, prompt="끝"),
            DialogNode(node_id="agent", kind=NodeKind.HANDOFF, prompt="연결"),
        ],
    )
    assert any("상담원" in p for p in validate(unreachable))


def test_폴백이_사람_연결이면_통과한다() -> None:
    """어디서 막혀도 빠져나갈 수 있다."""
    with_fallback = Scenario(
        scenario_id="ok",
        tenant_id="acme",
        entry_node="a",
        fallback_node="agent",
        nodes=[
            DialogNode(node_id="a", kind=NodeKind.END, prompt="끝"),
            DialogNode(node_id="agent", kind=NodeKind.HANDOFF, prompt="연결"),
        ],
    )
    assert validate(with_fallback) == []


def test_없는_노드를_가리키면_잡는다() -> None:
    broken = _scenario()
    broken.nodes[0].next_node = "nowhere"
    assert any("nowhere" in p for p in validate(broken))


def test_재질문_문구가_없으면_잡는다() -> None:
    broken = _scenario()
    node = broken.node("ask_purpose")
    assert node is not None
    node.reprompt = ""
    assert any("재질문" in p for p in validate(broken))


def test_인텐트_없는_ask를_잡는다() -> None:
    """답을 받아도 갈 곳이 없다."""
    broken = _scenario()
    node = broken.node("ask_purpose")
    assert node is not None
    node.intents = []
    assert any("인텐트" in p for p in validate(broken))


def test_잘못된_정규식을_잡는다() -> None:
    broken = _scenario()
    node = broken.node("ask_purpose")
    assert node is not None
    node.intents[0].patterns = ["([("]
    assert any("정규식" in p for p in validate(broken))


def test_빈_시나리오를_잡는다() -> None:
    empty = Scenario(scenario_id="x", tenant_id="acme", nodes=[])
    assert validate(empty) == ["노드가 없다"]


# --- 워커 ---------------------------------------------------------------


def _filter_result(text: str, *, is_final: bool = True) -> FilterResult:
    return FilterResult(
        session_id="s1",
        tenant_id="acme",
        seq=1,
        channel=ChannelRole.CUSTOMER,
        clean_text=text,
        is_final=is_final,
    )


async def test_고객_발화가_합성_요청으로_이어진다() -> None:
    bus = InMemoryEventBus()
    bus.register_group(Topic.TTS_REQUEST, "probe")
    bus.register_group(Topic.BOT_TURN, "probe")
    store = InMemoryDialogStore()
    await store.put_scenario(_scenario())

    worker = BotWorker(bus, store, group="bot", consumer="bot-1")
    await worker.handle(_filter_result("안녕하세요"))

    request = bus._queues[(Topic.TTS_REQUEST, "probe")].get_nowait()
    assert "어떤 도움이" in request.text
    turn = bus._queues[(Topic.BOT_TURN, "probe")].get_nowait()
    assert turn.node_id == "ask_purpose"


async def test_중간_인식으로는_대화를_진행하지_않는다() -> None:
    """고객이 말하는 도중에 봇이 끼어들어 답한다."""
    bus = InMemoryEventBus()
    bus.register_group(Topic.TTS_REQUEST, "probe")
    store = InMemoryDialogStore()
    await store.put_scenario(_scenario())

    worker = BotWorker(bus, store, group="bot", consumer="bot-1")
    await worker.handle(_filter_result("안녕", is_final=False))

    assert bus._queues[(Topic.TTS_REQUEST, "probe")].empty()


async def test_시나리오가_없으면_아무것도_하지_않는다() -> None:
    """사람 상담원 구성에서 봇이 끼어들면 안 된다."""
    bus = InMemoryEventBus()
    bus.register_group(Topic.TTS_REQUEST, "probe")
    worker = BotWorker(bus, InMemoryDialogStore(), group="bot", consumer="bot-1")
    await worker.handle(_filter_result("안녕하세요"))
    assert bus._queues[(Topic.TTS_REQUEST, "probe")].empty()


async def test_대화_상태가_저장소에_남는다() -> None:
    """봇 프로세스가 죽어도 통화가 이어지려면 상태가 밖에 있어야 한다."""
    bus = InMemoryEventBus()
    store = InMemoryDialogStore()
    await store.put_scenario(_scenario())

    worker = BotWorker(bus, store, group="bot", consumer="bot-1")
    await worker.handle(_filter_result("안녕하세요"))

    state = await store.get_state("s1")
    assert state is not None and state.current_node == "ask_purpose"

    # 새 워커(=재기동)가 이어받는다.
    fresh = BotWorker(bus, store, group="bot", consumer="bot-2")
    await fresh.handle(_filter_result("재발급이요"))
    resumed = await store.get_state("s1")
    assert resumed is not None and resumed.finished


# --- API ----------------------------------------------------------------


@pytest.fixture
async def client():
    app = create_app(store=InMemoryDialogStore(), bus=InMemoryEventBus())
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bot") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def test_문제가_있는_시나리오는_배포를_거부한다(client: httpx.AsyncClient) -> None:
    """막다른 골목이 운영에 올라가면 통화가 끊긴 뒤에야 알게 된다."""
    trap = Scenario(
        scenario_id="trap",
        tenant_id="acme",
        entry_node="a",
        nodes=[DialogNode(node_id="a", kind=NodeKind.END, prompt="끝")],
    )
    response = await client.put("/internal/v1/scenarios/acme", json=trap.model_dump(mode="json"))
    assert response.status_code == 400
    assert any("상담원" in p for p in response.json()["detail"]["problems"])


async def test_정상_시나리오는_배포된다(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/internal/v1/scenarios/acme", json=_scenario().model_dump(mode="json")
    )
    assert response.status_code == 200
    assert (await client.get("/internal/v1/scenarios/acme")).json()["published"] is True


async def test_시뮬레이터로_흐름을_확인한다(client: httpx.AsyncClient) -> None:
    """고객사가 "이렇게 말하면 어디로 가는지"를 스스로 볼 수 있어야 한다."""
    response = await client.post(
        "/internal/v1/scenarios/dry-run",
        json={"scenario": _scenario().model_dump(mode="json"), "utterances": ["분실했어요"]},
    )
    body = response.json()
    assert body["problems"] == []
    assert body["turns"][-1]["handed_off"] is True
    assert body["finished"] is True
