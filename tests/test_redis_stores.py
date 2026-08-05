"""Redis 구현이 인메모리와 **같게 동작하는가**.

단위 시험은 인메모리 구현으로 돈다. 그건 빠르지만, 운영에서 실제로 쓰이는
것은 Redis 쪽이다. 두 구현이 갈라지면 시험은 전부 초록인데 현장에서만
틀리고, 그 종류의 결함은 가장 늦게 발견된다.

여기서는 **저장소 계층만** 본다 — 왕복해서 같은 값이 나오는가, 상한이
지켜지는가, 정렬이 맞는가.

Redis가 없으면 건너뛴다. 개발 장비마다 있어야 하는 것은 아니지만, CI에는
있어야 이 시험이 의미가 있다.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

redis_asyncio = pytest.importorskip("redis.asyncio")

from vai_contracts.analytics import CallRecord, CallTurn, FallbackUtterance  # noqa: E402
from vai_contracts.dialog import Scenario, ScenarioRevision, ScenarioStage  # noqa: E402
from vai_core_adm.analytics import (  # noqa: E402
    InMemoryAnalyticsStore,
    RedisAnalyticsStore,
    overview,
)
from vai_scn_studio.lifecycle import (  # noqa: E402
    HISTORY_LIMIT,
    InMemoryScenarioStore,
    RedisScenarioStore,
)

REDIS_URL = os.environ.get("VAI_TEST_REDIS_URL", "redis://localhost:6379/15")
"""15번 DB를 쓴다. 개발용 데이터가 있는 0번을 건드리면 안 된다."""

TENANT = "t-redis"


async def _redis():  # type: ignore[no-untyped-def]
    client = redis_asyncio.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:  # 연결 실패 원인은 여러 가지다 — 이유를 가리지 않고 건너뛴다
        await client.aclose()
        pytest.skip(f"Redis 를 쓸 수 없다: {REDIS_URL}")
    return client


@pytest.fixture
async def redis():  # type: ignore[no-untyped-def]
    # 비동기 픽스처여야 한다. 동기 픽스처에서 asyncio.run 으로 만들면 클라이언트가
    # 그 자리에서 닫히는 이벤트 루프에 묶이고, 시험 본문에서 "루프가 닫혔다"가 난다.
    client = await _redis()
    # 이 시험의 키만 지운다. flushdb 는 같은 DB를 쓰는 다른 것까지 지운다.
    for pattern in ("vai:stats:*", "vai:authoring:scenario:*"):
        keys = await client.keys(pattern)
        if keys:
            await client.delete(*keys)
    yield client
    await client.aclose()


def scenario(version: int = 1) -> Scenario:
    return Scenario(
        scenario_id="s1",
        tenant_id=TENANT,
        version=version,
        entry_node="start",
        fallback_node="handoff",
    )


# ── 통계 ─────────────────────────────────────────────────────────────────────


async def test_일별_집계가_왕복한다(redis) -> None:  # type: ignore[no-untyped-def]
    store = RedisAnalyticsStore(redis, retention_days=1)
    await store.bump(TENANT, "2026-01-01", {"sessions": 2, "utterances": 5})
    await store.bump(TENANT, "2026-01-01", {"sessions": 1, "confidence_sum": 1.5})

    rows = await store.daily(TENANT, ["2026-01-01", "2026-01-02"])

    assert rows[0].sessions == 3
    assert rows[0].utterances == 5
    assert rows[0].confidence_sum == pytest.approx(1.5)
    assert rows[1].sessions == 0, "기록이 없는 날도 0으로 채워져야 그래프가 안 끊긴다"


async def test_실수와_정수를_섞어도_깨지지_않는다(redis) -> None:  # type: ignore[no-untyped-def]
    """신뢰도만 실수다. 같은 해시에 정수 칸과 섞여 들어간다."""
    store = RedisAnalyticsStore(redis, retention_days=1)
    for _ in range(3):
        await store.bump(TENANT, "2026-01-01", {"confidence_sum": 0.85, "confidence_n": 1})

    rows = await store.daily(TENANT, ["2026-01-01"])

    assert rows[0].confidence_n == 3
    assert rows[0].mean_confidence == pytest.approx(0.85)


async def test_두_구현이_같은_기간_합계를_낸다(redis) -> None:  # type: ignore[no-untyped-def]
    """갈라지면 시험은 초록인데 현장에서만 틀린다."""
    days = ["2026-01-01", "2026-01-02"]
    payloads = [
        {"sessions": 4, "confidence_sum": 3.6, "confidence_n": 4, "summaries_ready": 2},
        {"sessions": 1, "confidence_sum": 0.5, "confidence_n": 1, "summaries_failed": 1},
    ]
    live = RedisAnalyticsStore(redis, retention_days=1)
    memory = InMemoryAnalyticsStore()
    for day, payload in zip(days, payloads, strict=True):
        await live.bump(TENANT, day, payload)
        await memory.bump(TENANT, day, payload)

    from_live = overview(TENANT, await live.daily(TENANT, days))
    from_memory = overview(TENANT, await memory.daily(TENANT, days))

    assert from_live.mean_confidence == pytest.approx(from_memory.mean_confidence)
    assert from_live.summary_success_rate == pytest.approx(from_memory.summary_success_rate)
    assert from_live.sessions == from_memory.sessions == 5


async def test_랭킹이_많은_순으로_나온다(redis) -> None:  # type: ignore[no-untyped-def]
    store = RedisAnalyticsStore(redis, retention_days=1)
    for _ in range(3):
        await store.rank(TENANT, "rule", "R-01", "주민번호 요구 금지")
    await store.rank(TENANT, "rule", "R-02")

    ranks = await store.rankings(TENANT, "rule")

    assert [(r.key, r.count) for r in ranks] == [("R-01", 3), ("R-02", 1)]
    assert ranks[0].label == "주민번호 요구 금지"


async def test_같은_폴백은_횟수로_모인다(redis) -> None:  # type: ignore[no-untyped-def]
    store = RedisAnalyticsStore(redis, retention_days=1)
    for _ in range(2):
        await store.add_fallback(TENANT, FallbackUtterance(text="법인 계좌 압류 해제"))

    items = await store.fallbacks(TENANT)

    assert len(items) == 1
    assert items[0].count == 2


async def test_이력이_최신순으로_나오고_내용으로_검색된다(redis) -> None:  # type: ignore[no-untyped-def]
    store = RedisAnalyticsStore(redis, retention_days=1)
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

    assert [c.session_id for c in await store.list_calls(TENANT)] == ["s2", "s1"]
    assert [c.session_id for c in await store.list_calls(TENANT, query="재발급")] == ["s1"]


async def test_이력_파기는_목록에서도_사라진다(redis) -> None:  # type: ignore[no-untyped-def]
    """본문만 지우고 색인을 남기면 목록에 유령 항목이 남는다."""
    store = RedisAnalyticsStore(redis, retention_days=1)
    await store.put_call(CallRecord(session_id="s1", tenant_id=TENANT))

    assert await store.delete_call("s1")
    assert await store.list_calls(TENANT) == []
    assert not await store.delete_call("s1"), "없는 것을 지웠다고 하면 안 된다"


async def test_보관기간_ttl이_실제로_걸린다(redis) -> None:  # type: ignore[no-untyped-def]
    """TTL의 이유는 용량이 아니라 개인정보다. 안 걸리면 영원히 남는다."""
    store = RedisAnalyticsStore(redis, retention_days=2)
    await store.bump(TENANT, "2026-01-01", {"sessions": 1})
    await store.put_call(CallRecord(session_id="s1", tenant_id=TENANT))

    for key in (f"vai:stats:daily:{TENANT}:2026-01-01", "vai:stats:call:s1"):
        ttl = await redis.ttl(key)
        assert 0 < ttl <= 2 * 86400, f"{key} 에 TTL 이 없다"


# ── 시나리오 ─────────────────────────────────────────────────────────────────


async def test_시나리오_판이_왕복한다(redis) -> None:  # type: ignore[no-untyped-def]
    store = RedisScenarioStore(redis)
    await store.put(
        TENANT, ScenarioRevision(scenario=scenario(), stage=ScenarioStage.BUILT, note="첫 판")
    )

    found = await store.get(TENANT)

    assert found is not None
    assert found.stage is ScenarioStage.BUILT
    assert found.note == "첫 판"


async def test_배포_이력이_최신순이고_상한이_있다(redis) -> None:  # type: ignore[no-untyped-def]
    """이력이 무한히 쌓이면 조회가 느려지고, 되돌리기는 최근 것만 쓴다."""
    store = RedisScenarioStore(redis)
    for version in range(1, HISTORY_LIMIT + 6):
        await store.push_history(TENANT, ScenarioRevision(scenario=scenario(version)))

    history = await store.history(TENANT)

    assert len(history) == HISTORY_LIMIT
    assert history[0].scenario.version == HISTORY_LIMIT + 5, "최신이 앞에 와야 한다"


async def test_두_시나리오_구현이_같은_이력_순서를_낸다(redis) -> None:  # type: ignore[no-untyped-def]
    live = RedisScenarioStore(redis)
    memory = InMemoryScenarioStore()
    for version in (1, 2, 3):
        revision = ScenarioRevision(scenario=scenario(version))
        await live.push_history(TENANT, revision)
        await memory.push_history(TENANT, revision)

    assert [r.scenario.version for r in await live.history(TENANT)] == [
        r.scenario.version for r in await memory.history(TENANT)
    ]
