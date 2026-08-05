"""운영 통계가 **맞는 숫자**를 내는가.

틀린 지표는 지표가 없는 것보다 나쁘다. 없으면 아무도 안 쓰지만, 틀리면
그걸 근거로 증설하고 튜닝하고 제안서에 적는다.

여기서 확인하는 것:

* 기간을 합칠 때 평균이 무너지지 않는가 (평균의 평균 함정)
* 중간 인식 결과가 발화 수를 부풀리지 않는가
* 이력에 **마스킹본만** 담기는가
* 세션 상태가 끝날 때 버려지는가 (24시간 도는 온프렘에서 새면 죽는다)
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import pytest

from vai_common.bus import InMemoryEventBus
from vai_contracts.analytics import CallRecord, CallTurn, DailyStats, FallbackUtterance
from vai_contracts.dialog import BotTurn
from vai_contracts.events import FilterResult, MatchedRule, SessionClosed, SttDelta
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.summary import SummaryDone, SummaryStatus
from vai_contracts.topics import Topic
from vai_core_adm.analytics import (
    MAX_TURNS,
    InMemoryAnalyticsStore,
    build_collectors,
    day_key,
    normalize_utterance,
    overview,
    recent_days,
)

SESSION = "sess_an_0001"
TENANT = "t1"


# ── 집계 산수 ────────────────────────────────────────────────────────────────


def test_기간_합계는_평균의_평균이_아니다() -> None:
    """통화 100건인 날과 1건인 날의 평균을 그냥 더해 2로 나누면 틀린다."""
    rows = [
        DailyStats(day="2026-01-01", confidence_sum=90.0, confidence_n=100),
        DailyStats(day="2026-01-02", confidence_sum=0.5, confidence_n=1),
    ]

    total = overview(TENANT, rows)

    assert total.mean_confidence == pytest.approx(90.5 / 101)
    assert total.mean_confidence != pytest.approx((0.9 + 0.5) / 2)


def test_요약_성공률은_끝난_건만_분모로_쓴다() -> None:
    rows = [DailyStats(day="2026-01-01", summaries_ready=3, summaries_failed=1)]

    assert overview(TENANT, rows).summary_success_rate == pytest.approx(0.75)


def test_아무_일도_없으면_0으로_나누지_않는다() -> None:
    total = overview(TENANT, [DailyStats(day="2026-01-01")])

    assert total.mean_confidence == 0.0
    assert total.summary_success_rate == 0.0
    assert total.handoff_rate == 0.0


def test_기간은_오래된_날이_앞에_온다() -> None:
    days = recent_days(3, now=datetime(2026, 1, 3, 12, tzinfo=UTC))

    assert days == ["2026-01-01", "2026-01-02", "2026-01-03"]


def test_모르는_칸이_있어도_조회가_깨지지_않는다() -> None:
    """예전 버전이 쓴 칸이 남았다고 화면이 통째로 비면 안 된다."""

    async def go() -> list[DailyStats]:
        store = InMemoryAnalyticsStore()
        await store.bump(TENANT, "2026-01-01", {"sessions": 2, "왕년의_지표": 9})
        return await store.daily(TENANT, ["2026-01-01"])

    rows = asyncio.run(go())

    assert rows[0].sessions == 2


def test_폴백_묶음_키는_공백만_흡수한다() -> None:
    assert normalize_utterance("  카드  재발급   ") == "카드 재발급"
    assert normalize_utterance("카드재발급") != normalize_utterance("카드 재발급")


# ── 수집 ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def bus() -> InMemoryEventBus:
    bus = InMemoryEventBus()
    for topic, group in [
        (Topic.FILTER_CLEAN, "adm:analytics:utterance"),
        (Topic.STT_DELTA, "adm:analytics:stt"),
        (Topic.BOT_TURN, "adm:analytics:bot"),
        (Topic.SUMMARY_DONE, "adm:analytics:summary"),
        (Topic.SESSION_CLOSED, "adm:analytics:session"),
    ]:
        bus.register_group(topic, group)
    return bus


def clean(
    text: str,
    *,
    channel: ChannelRole = ChannelRole.CUSTOMER,
    pii: bool = False,
    rules: list[MatchedRule] | None = None,
    start_ms: int = 0,
) -> FilterResult:
    return FilterResult(
        session_id=SESSION,
        tenant_id=TENANT,
        channel=channel,
        clean_text=text,
        pii_masked=pii,
        matched_rules=rules or [],
        is_final=True,
        start_ms=start_ms,
    )


class Harness:
    def __init__(self, bus: InMemoryEventBus) -> None:
        self.store = InMemoryAnalyticsStore()
        self.workers, self.acc = build_collectors(
            bus, self.store, group="adm:analytics", consumer="adm-1"
        )
        self._tasks: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> Harness:
        self._tasks = [asyncio.create_task(w.run()) for w in self.workers]
        return self

    async def __aexit__(self, *_: object) -> None:
        for worker in self.workers:
            worker.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def settle(self, rounds: int = 20) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0.005)


async def test_확정_발화만_세고_중간_결과는_안_센다(bus: InMemoryEventBus) -> None:
    async with Harness(bus) as h:
        await bus.publish(Topic.FILTER_CLEAN, clean("결제일 연기 되나요?"))
        interim = clean("결제일 연")
        interim.is_final = False
        await bus.publish(Topic.FILTER_CLEAN, interim)
        await h.settle()

        rows = await h.store.daily(TENANT, [day_key()])

    assert rows[0].utterances == 1


async def test_마스킹과_룰_적중이_따로_잡힌다(bus: InMemoryEventBus) -> None:
    """마스킹 건수가 계속 0이면 마스킹이 꺼진 것을 의심할 수 있어야 한다."""
    async with Harness(bus) as h:
        await bus.publish(
            Topic.FILTER_CLEAN,
            clean(
                "주민번호는 [주민등록번호]입니다",
                pii=True,
                rules=[MatchedRule(rule_id="R-01", message="주민번호 요구 금지")],
            ),
        )
        await h.settle()

        rows = await h.store.daily(TENANT, [day_key()])
        ranks = await h.store.rankings(TENANT, "rule")

    assert rows[0].pii_masked == 1
    assert rows[0].rule_hits == 1
    assert ranks[0].key == "R-01"
    assert ranks[0].label == "주민번호 요구 금지"


async def test_인식_신뢰도는_확정분만_평균한다(bus: InMemoryEventBus) -> None:
    async with Harness(bus) as h:
        for confidence, final in [(0.9, True), (0.1, False), (0.7, True)]:
            await bus.publish(
                Topic.STT_DELTA,
                SttDelta(
                    session_id=SESSION,
                    tenant_id=TENANT,
                    channel=ChannelRole.CUSTOMER,
                    text="안녕하세요",
                    is_final=final,
                    confidence=confidence,
                    duration_ms=1000,
                ),
            )
        await h.settle()

        rows = await h.store.daily(TENANT, [day_key()])

    assert rows[0].confidence_n == 2
    assert rows[0].mean_confidence == pytest.approx(0.8)
    assert rows[0].audio_ms == 2000


async def test_세션이_끝나야_이력_한_건이_생긴다(bus: InMemoryEventBus) -> None:
    async with Harness(bus) as h:
        await bus.publish(Topic.FILTER_CLEAN, clean("결제일 연기 되나요?"))
        await h.settle()

        assert await h.store.list_calls(TENANT) == [], "진행 중인 세션이 이력에 뜨면 안 된다"

        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(
                session_id=SESSION,
                tenant_id=TENANT,
                profile=SessionProfile.AICC,
                duration_ms=42_000,
            ),
        )
        await h.settle()

        calls = await h.store.list_calls(TENANT)

    assert len(calls) == 1
    assert calls[0].duration_ms == 42_000
    assert calls[0].turns[0].text == "결제일 연기 되나요?"


async def test_발화_없는_세션은_이력에_남기지_않는다(bus: InMemoryEventBus) -> None:
    """오접속·즉시 끊김이 이력 목록을 쓰레기로 채우면 조회가 무의미해진다."""
    async with Harness(bus) as h:
        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(session_id=SESSION, tenant_id=TENANT, profile=SessionProfile.AICC),
        )
        await h.settle()

        rows = await h.store.daily(TENANT, [day_key()])
        calls = await h.store.list_calls(TENANT)

    assert rows[0].sessions == 1, "세션 수는 세야 한다"
    assert calls == [], "이력에는 남기지 않는다"


async def test_세션이_끝나면_진행중_상태를_버린다(bus: InMemoryEventBus) -> None:
    """안 버리면 24시간 도는 온프렘에서 메모리가 조용히 늘다가 죽는다."""
    async with Harness(bus) as h:
        await bus.publish(Topic.FILTER_CLEAN, clean("한 마디"))
        await h.settle()
        assert SESSION in h.acc.records

        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(session_id=SESSION, tenant_id=TENANT, profile=SessionProfile.AICC),
        )
        await h.settle()

        assert SESSION not in h.acc.records
        assert SESSION not in h.acc.last_customer


async def test_이력_발화는_상한에서_잘리고_잘렸다고_적는다(bus: InMemoryEventBus) -> None:
    async with Harness(bus) as h:
        for index in range(MAX_TURNS + 5):
            await bus.publish(Topic.FILTER_CLEAN, clean(f"발화 {index}", start_ms=index))
        await h.settle(rounds=200)
        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(session_id=SESSION, tenant_id=TENANT, profile=SessionProfile.AICC),
        )
        await h.settle()

        record = await h.store.get_call(SESSION)

    assert record is not None
    assert len(record.turns) == MAX_TURNS
    assert record.truncated, "잘렸다는 사실을 숨기면 이력을 증거로 쓸 수 없다"


async def test_사람에게_넘어가면_직전_고객_발화가_재학습_후보가_된다(
    bus: InMemoryEventBus,
) -> None:
    async with Harness(bus) as h:
        await bus.publish(Topic.FILTER_CLEAN, clean("법인 계좌 압류 해제 어떻게 하나요"))
        await h.settle()
        await bus.publish(
            Topic.BOT_TURN,
            BotTurn(
                session_id=SESSION,
                tenant_id=TENANT,
                turn_id=f"{SESSION}:1",
                node_id="fallback",
                text="상담원을 연결하겠습니다",
                handed_off=True,
            ),
        )
        await h.settle()

        items = await h.store.fallbacks(TENANT)
        rows = await h.store.daily(TENANT, [day_key()])

    assert [i.text for i in items] == ["법인 계좌 압류 해제 어떻게 하나요"]
    assert items[0].node_id == "fallback"
    assert rows[0].handoffs == 1
    assert rows[0].bot_turns == 1


async def test_넘기지_않은_봇_턴은_재학습_후보가_아니다(bus: InMemoryEventBus) -> None:
    """정상 처리된 발화까지 재학습 목록에 들어가면 목록이 쓸모없어진다."""
    async with Harness(bus) as h:
        await bus.publish(Topic.FILTER_CLEAN, clean("잔액 알려주세요"))
        await h.settle()
        await bus.publish(
            Topic.BOT_TURN,
            BotTurn(
                session_id=SESSION,
                tenant_id=TENANT,
                turn_id=f"{SESSION}:1",
                node_id="balance",
                text="잔액은 1만원입니다",
            ),
        )
        await h.settle()

        assert await h.store.fallbacks(TENANT) == []
        assert (await h.store.rankings(TENANT, "node"))[0].key == "balance"


async def test_요약_결과가_이력에_붙는다(bus: InMemoryEventBus) -> None:
    """요약은 세션 종료 뒤에 끝난다. 결과가 이력에 안 붙으면 조회 화면이
    '요약 없음'으로 남는다."""
    async with Harness(bus) as h:
        await bus.publish(Topic.FILTER_CLEAN, clean("한 마디"))
        await h.settle()
        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(session_id=SESSION, tenant_id=TENANT, profile=SessionProfile.AICC),
        )
        await h.settle()
        await bus.publish(
            Topic.SUMMARY_DONE,
            SummaryDone(
                session_id=SESSION,
                tenant_id=TENANT,
                profile=SessionProfile.AICC,
                status=SummaryStatus.READY,
                latency_ms=1200,
                waited_for_stt_ms=3000,
            ),
        )
        await h.settle()

        record = await h.store.get_call(SESSION)
        rows = await h.store.daily(TENANT, [day_key()])

    assert record is not None
    assert record.summary_status == "ready"
    assert rows[0].summaries_ready == 1
    assert rows[0].summary_waited_sum == 3000


async def test_요약_실패도_센다(bus: InMemoryEventBus) -> None:
    """실패를 안 세면 성공률이 항상 100%로 보인다."""
    async with Harness(bus) as h:
        await bus.publish(
            Topic.SUMMARY_DONE,
            SummaryDone(
                session_id=SESSION,
                tenant_id=TENANT,
                profile=SessionProfile.AICC,
                status=SummaryStatus.FAILED,
            ),
        )
        await h.settle()

        rows = await h.store.daily(TENANT, [day_key()])

    assert rows[0].summaries_failed == 1
    assert overview(TENANT, rows).summary_success_rate == 0.0


# ── 이력 조회 ────────────────────────────────────────────────────────────────


async def test_이력_검색은_대화_내용도_본다() -> None:
    store = InMemoryAnalyticsStore()
    await store.put_call(
        CallRecord(
            session_id="s1",
            tenant_id=TENANT,
            turns=[CallTurn(text="카드 재발급 문의")],
            ended_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await store.put_call(
        CallRecord(
            session_id="s2",
            tenant_id=TENANT,
            turns=[CallTurn(text="대출 상담")],
            ended_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )

    assert [c.session_id for c in await store.list_calls(TENANT, query="재발급")] == ["s1"]
    assert [c.session_id for c in await store.list_calls(TENANT)] == ["s2", "s1"], (
        "최근 것이 먼저 와야 한다"
    )


async def test_같은_폴백_발화는_횟수로_모인다() -> None:
    store = InMemoryAnalyticsStore()
    for _ in range(3):
        await store.add_fallback(
            TENANT, FallbackUtterance(text="법인 계좌  압류 해제", session_id="s")
        )

    items = await store.fallbacks(TENANT)

    assert len(items) == 1
    assert items[0].count == 3, "반복되는 질문이 우선순위가 높다"


async def test_이력_발화가_시간순으로_정렬된다(bus: InMemoryEventBus) -> None:
    """고객 발화와 봇 발화는 다른 워커가 모은다. 도착 순서를 그대로 두면
    대화가 앞뒤로 튀고, 그런 이력은 읽을 수 없다."""
    async with Harness(bus) as h:
        # 뒤쪽 발화가 먼저 도착한다(다른 워커·다른 지연).
        await bus.publish(Topic.FILTER_CLEAN, clean("그리고 한도도요", start_ms=5000))
        await h.settle()
        await bus.publish(
            Topic.BOT_TURN,
            BotTurn(
                session_id=SESSION,
                tenant_id=TENANT,
                turn_id=f"{SESSION}:1",
                node_id="greet",
                text="무엇을 도와드릴까요",
            ),
        )
        await h.settle()
        await bus.publish(Topic.FILTER_CLEAN, clean("카드 재발급이요", start_ms=0))
        await h.settle()
        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(session_id=SESSION, tenant_id=TENANT, profile=SessionProfile.AICC),
        )
        await h.settle()

        record = await h.store.get_call(SESSION)

    assert record is not None
    assert [turn.text for turn in record.turns] == [
        "카드 재발급이요",
        "무엇을 도와드릴까요",
        "그리고 한도도요",
    ]


def test_종료되지_않은_세션이_무한히_쌓이지_않는다(caplog: pytest.LogCaptureFixture) -> None:
    """종료 이벤트가 끝내 오지 않는 세션이 있다. 통계 화면 때문에 상담이
    멈추는 일은 없어야 한다."""
    from vai_core_adm.analytics import _Accumulator

    acc = _Accumulator(max_open=3)
    with caplog.at_level("WARNING"):
        for index in range(5):
            acc.get(f"s{index}", TENANT)

    assert len(acc.records) == 3
    assert "s0" not in acc.records, "가장 오래된 것부터 버린다"
    assert "s4" in acc.records
    assert any("상한" in record.message for record in caplog.records), "조용히 버리면 안 된다"
