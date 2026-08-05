"""양보 규칙 시험.

이 모듈이 지키는 것은 성능이 아니라 **순서**다. 시험도 그것을 본다:
실시간이 먼저인가, 배치가 영영 밀리지는 않는가, 끌 수 있는가.
"""

from __future__ import annotations

import asyncio

import pytest

from vai_common.resource import Heartbeat, MemoryBusyRegistry, Yield


@pytest.mark.asyncio
async def test_한가하면_기다리지_않는다() -> None:
    result = await Yield(MemoryBusyRegistry(), poll_interval_s=0.01).wait_for_turn()

    assert result == pytest.approx(0.0, abs=0.05)


@pytest.mark.asyncio
async def test_바쁘면_풀릴_때까지_기다린다() -> None:
    registry = MemoryBusyRegistry()
    await registry.mark_busy(ttl_s=60)
    yielder = Yield(registry, poll_interval_s=0.01, max_wait_s=5.0)

    async def release() -> None:
        await asyncio.sleep(0.05)
        await registry.clear()

    releaser = asyncio.create_task(release())
    waited = await yielder.wait_for_turn()
    await releaser

    assert waited > 0.0
    assert not yielder.gave_up


@pytest.mark.asyncio
async def test_상한을_넘기면_포기하고_진행한다() -> None:
    """가장 중요한 시험이다.

    통화가 끊이지 않는 상담센터에서는 "STT가 한가해질 때"가 영영 오지 않는다.
    여기서 계속 기다리면 회의록이 아예 안 나오고, 그건 늦는 것과 다른 문제다.
    """
    registry = MemoryBusyRegistry()
    await registry.mark_busy(ttl_s=600)
    yielder = Yield(registry, poll_interval_s=0.01, max_wait_s=0.05)

    waited = await yielder.wait_for_turn()

    assert yielder.gave_up
    assert waited >= 0.05


@pytest.mark.asyncio
async def test_꺼두면_즉시_진행한다() -> None:
    registry = MemoryBusyRegistry()
    await registry.mark_busy(ttl_s=600)

    assert await Yield(registry, enabled=False).wait_for_turn() == 0.0


@pytest.mark.asyncio
async def test_ttl_이_지나면_저절로_풀린다() -> None:
    """프로세스가 죽어도 표시가 남으면 요약이 영원히 대기한다."""
    registry = MemoryBusyRegistry()
    await registry.mark_busy(ttl_s=0)

    assert not await registry.is_busy()


@pytest.mark.asyncio
async def test_하트비트는_간격_안에서_다시_찍지_않는다() -> None:
    """발화 하나에 한 번씩 Redis 를 때리면 그 자체가 지연이 된다."""
    calls: list[int] = []

    class Counting(MemoryBusyRegistry):
        async def mark_busy(self, ttl_s: int = 20) -> None:
            calls.append(ttl_s)
            await super().mark_busy(ttl_s)

    beat = Heartbeat(Counting(), refresh_interval_s=60.0)
    await beat.touch()
    await beat.touch()
    await beat.touch()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_하트비트를_끄면_아무것도_안_한다() -> None:
    registry = MemoryBusyRegistry()
    await Heartbeat(registry, enabled=False).touch()

    assert not await registry.is_busy()
