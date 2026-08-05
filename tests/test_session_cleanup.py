"""세션 상태가 실제로 해제되는가 — 레포 전체 게이트.

``release_session``을 **정의만 하고 등록하지 않으면** 그 워커는 조용히 샌다.
24시간 도는 온프렘에서 메모리가 천천히 늘다가 어느 날 죽고, 그때는 원인이
몇 달 전 커밋에 있다. 실제로 네 워커 중 하나만 리퍼에 연결돼 있었다.

여기서 두 가지를 본다:

1. 세션 상태를 가진 워커가 리퍼에 등록돼 있는가(정적 검사).
2. 리퍼가 실제로 상태를 비우는가(동작 검사).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from vai_common.bus import InMemoryEventBus
from vai_common.worker import BlockWorker, SessionReaper
from vai_contracts.events import BaseEvent, SessionClosed
from vai_contracts.session import SessionProfile
from vai_contracts.topics import Topic

REPO = Path(__file__).resolve().parents[1]
BLOCKS = REPO / "blocks"


def _blocks_overriding_release() -> list[str]:
    """``release_session``을 재정의한 블록 디렉토리 이름."""
    found = []
    for worker in sorted(BLOCKS.glob("*/src/*/worker.py")):
        if "def release_session" in worker.read_text(encoding="utf-8"):
            found.append(worker.parents[2].name)
    return found


def test_every_worker_with_session_state_is_reaped() -> None:
    """상태를 가진 워커는 반드시 SessionReaper에 등록돼야 한다.

    등록을 빠뜨려도 테스트는 통과하고 기능도 정상으로 보인다 — 그래서
    사람 눈으로는 못 잡는다. 이 검사가 그 자리를 대신한다.
    """
    unreaped = []
    for block in _blocks_overriding_release():
        main = next((BLOCKS / block / "src").glob("*/main.py"), None)
        entry = main.read_text(encoding="utf-8") if main else ""
        app = next((BLOCKS / block / "src").glob("*/app.py"), None)
        wiring = entry + (app.read_text(encoding="utf-8") if app else "")
        # import만 남아 있고 호출이 사라진 경우를 걸러야 한다 — 이름 존재가
        # 아니라 **호출**을 본다.
        if "SessionReaper(" not in wiring:
            unreaped.append(block)

    assert not unreaped, (
        f"release_session을 정의했지만 리퍼에 등록되지 않았다: {unreaped} — "
        "정의만 하면 세션 상태가 영원히 남는다"
    )


def test_reaper_groups_are_unique_per_block() -> None:
    """``session.closed``는 여러 블록이 소비한다.

    컨슈머 그룹을 공유하면 둘 중 하나만 메시지를 받고, 못 받은 쪽은 영원히
    해제되지 않는다 — 그리고 그 사실이 오류로 드러나지 않는다.
    """
    pattern = re.compile(r'group=f?"\{?[^"]*?\}?:([a-z-]+reaper)"')
    groups: list[str] = []
    for main in sorted(BLOCKS.glob("*/src/*/main.py")):
        groups.extend(pattern.findall(main.read_text(encoding="utf-8")))

    assert groups, "리퍼 그룹을 하나도 못 찾았다 — 패턴이 낡았다"
    assert len(groups) == len(set(groups)), f"리퍼 컨슈머 그룹이 겹친다: {groups}"


# ── 동작 검사 ────────────────────────────────────────────────────────────────


class _StatefulWorker(BlockWorker[BaseEvent]):
    block_id = "TEST"
    source_topic = Topic.AUDIO_IN
    source_model = BaseEvent

    def __init__(self, bus: InMemoryEventBus) -> None:
        super().__init__(bus, group="test", consumer="test-1")
        self.state: dict[str, str] = {}

    async def handle(self, event: BaseEvent) -> None:
        return None

    def release_session(self, session_id: str) -> None:
        self.state.pop(session_id, None)


class _AngryWorker(_StatefulWorker):
    block_id = "ANGRY"

    def release_session(self, session_id: str) -> None:
        raise RuntimeError("해제 실패")


async def _reap(bus: InMemoryEventBus, workers: list[BlockWorker[BaseEvent]]) -> None:
    reaper = SessionReaper(bus, workers, group="test:reaper")
    bus.register_group(Topic.SESSION_CLOSED, "test:reaper")
    task = asyncio.create_task(reaper.run())
    await bus.publish(
        Topic.SESSION_CLOSED,
        SessionClosed(session_id="s1", tenant_id="t1", seq=1, profile=SessionProfile.MEETING),
    )
    await asyncio.sleep(0.05)
    reaper.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_reaper_clears_state_on_session_close() -> None:
    bus = InMemoryEventBus()
    worker = _StatefulWorker(bus)
    worker.state["s1"] = "회의 중"
    worker.state["s2"] = "다른 회의"

    await _reap(bus, [worker])

    assert "s1" not in worker.state
    assert "s2" in worker.state, "끝나지 않은 세션까지 지우면 진행 중인 통화가 깨진다"


async def test_one_failing_worker_does_not_block_the_others() -> None:
    """해제 실패로 재처리 루프에 빠지면 그게 더 큰 누수다."""
    bus = InMemoryEventBus()
    angry = _AngryWorker(bus)
    calm = _StatefulWorker(bus)
    calm.state["s1"] = "회의 중"

    await _reap(bus, [angry, calm])

    assert "s1" not in calm.state


def test_base_worker_release_is_a_safe_noop() -> None:
    """상태 없는 워커도 리퍼에 넣을 수 있어야 한다 — 등록을 망설일 이유를 없앤다."""

    class Plain(_StatefulWorker):
        pass

    plain = Plain(InMemoryEventBus())
    BlockWorker.release_session(plain, "아무거나")  # 예외가 나오면 실패


@pytest.mark.parametrize("block", _blocks_overriding_release())
def test_release_actually_pops_something(block: str) -> None:
    """빈 구현이 되어 있으면 등록해도 아무 일이 없다."""
    worker = next((BLOCKS / block / "src").glob("*/worker.py"))
    body = worker.read_text(encoding="utf-8").split("def release_session", 1)[1]
    assert "pop(" in body.split("\n\n", 1)[0], f"{block}: release_session이 아무것도 비우지 않는다"
