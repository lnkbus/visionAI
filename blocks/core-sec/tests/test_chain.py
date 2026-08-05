"""해시 체인 감사 로그.

감사 로그의 가치는 "변조되지 않았음을 증명할 수 있는가"에 있다. 그래서
정상 동작보다 **변조를 실제로 잡아내는지**를 더 촘촘히 검증한다.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vai_common.bus import InMemoryEventBus
from vai_contracts.audit import AuditAction, AuditEvent, AuditOutcome
from vai_contracts.topics import Topic
from vai_core_sec.app import create_app
from vai_core_sec.chain import GENESIS_HASH, AuditChain, segment_name
from vai_core_sec.worker import AuditWorker


def _event(actor: str = "operator1", action: AuditAction = AuditAction.PII_REVEAL) -> AuditEvent:
    return AuditEvent(
        actor=actor,
        action=action,
        resource="sess_123",
        session_id="sess_123",
        tenant_id="acme",
        block_id="UI-AGENT",
    )


@pytest.fixture
def chain(tmp_path: Path) -> AuditChain:
    store = AuditChain(tmp_path / "audit")
    store.load()
    return store


# --- 기록 ---------------------------------------------------------------


async def test_첫_레코드는_제네시스에_이어진다(chain: AuditChain) -> None:
    record = await chain.append(_event())
    assert record.seq_no == 1
    assert record.prev_hash == GENESIS_HASH
    assert len(record.entry_hash) == 64


async def test_레코드가_앞_레코드에_이어진다(chain: AuditChain) -> None:
    first = await chain.append(_event())
    second = await chain.append(_event(actor="operator2"))
    assert second.prev_hash == first.entry_hash
    assert second.seq_no == 2


async def test_재기동해도_체인이_이어진다(tmp_path: Path) -> None:
    """프로세스가 죽고 다시 떠도 일련번호가 1로 돌아가면 안 된다 —
    그 순간 앞 기록이 통째로 의심받는다."""
    first_run = AuditChain(tmp_path / "audit")
    first_run.load()
    await first_run.append(_event())
    await first_run.append(_event())

    second_run = AuditChain(tmp_path / "audit")
    second_run.load()
    third = await second_run.append(_event())
    assert third.seq_no == 3
    assert second_run.verify().intact


async def test_감사_디렉토리는_소유자만_본다(tmp_path: Path) -> None:
    """감사 로그가 열려 있으면 그 자체가 유출 경로다."""
    directory = tmp_path / "audit"
    AuditChain(directory).load()
    assert directory.stat().st_mode & 0o077 == 0


async def test_날짜별_세그먼트에_쌓인다(chain: AuditChain) -> None:
    await chain.append(_event())
    assert [p.name for p in chain.segments()] == [segment_name(datetime.now(UTC).date())]


# --- 변조 탐지 -----------------------------------------------------------


async def test_정상_체인은_검증을_통과한다(chain: AuditChain) -> None:
    for _ in range(5):
        await chain.append(_event())
    status = chain.verify()
    assert status.intact and status.total == 5
    assert status.head_hash == chain.head_hash


async def test_내용을_고치면_그_레코드를_짚어_준다(chain: AuditChain) -> None:
    """가장 흔한 은폐: 열람 기록의 행위자를 다른 사람으로 바꾸는 것."""
    for _ in range(3):
        await chain.append(_event())

    segment = chain.segments()[0]
    lines = segment.read_text(encoding="utf-8").splitlines()
    doctored = json.loads(lines[1])
    doctored["event"]["actor"] = "somebody_else"
    lines[1] = json.dumps(doctored, ensure_ascii=False)
    segment.write_text("\n".join(lines) + "\n", encoding="utf-8")

    status = chain.verify()
    assert not status.intact
    assert status.broken_at == 2
    assert "변조" in status.reason


async def test_한_줄을_지우면_삭제를_탐지한다(chain: AuditChain) -> None:
    """지우고 나머지를 그대로 두는 방식. 일련번호가 비어 바로 드러난다."""
    for _ in range(4):
        await chain.append(_event())

    segment = chain.segments()[0]
    lines = segment.read_text(encoding="utf-8").splitlines()
    del lines[1]
    segment.write_text("\n".join(lines) + "\n", encoding="utf-8")

    status = chain.verify()
    assert not status.intact
    assert status.broken_at == 3
    assert "삭제" in status.reason


async def test_해시까지_다시_계산해_넣어도_앞_고리에서_걸린다(chain: AuditChain) -> None:
    """단순 변조보다 정교한 시도: 레코드를 고치고 자기 해시도 맞춰 넣는다.
    그래도 **다음** 레코드의 prev_hash가 어긋나 체인이 끊긴다."""
    from vai_contracts.audit import AuditRecord
    from vai_core_sec.chain import _digest

    for _ in range(3):
        await chain.append(_event())

    segment = chain.segments()[0]
    lines = segment.read_text(encoding="utf-8").splitlines()
    record = AuditRecord.model_validate_json(lines[1])
    record.event.actor = "somebody_else"
    record.entry_hash = _digest(record)  # 자기 해시는 맞춰 놓는다
    lines[1] = record.model_dump_json()
    segment.write_text("\n".join(lines) + "\n", encoding="utf-8")

    status = chain.verify()
    assert not status.intact
    assert status.broken_at == 3  # 다음 레코드에서 끊긴다
    assert "앞 레코드" in status.reason


async def test_빈_저장소는_온전한_것으로_본다(chain: AuditChain) -> None:
    status = chain.verify()
    assert status.intact and status.total == 0


# --- 조회 ---------------------------------------------------------------


async def test_행위자와_행위로_거른다(chain: AuditChain) -> None:
    await chain.append(_event(actor="kim", action=AuditAction.PII_REVEAL))
    await chain.append(_event(actor="lee", action=AuditAction.PII_REVEAL))
    await chain.append(_event(actor="kim", action=AuditAction.EXPORT))

    assert len(chain.query(actor="kim")) == 2
    assert len(chain.query(action=AuditAction.PII_REVEAL)) == 2
    assert len(chain.query(actor="kim", action=AuditAction.EXPORT)) == 1


async def test_최신_기록부터_돌려준다(chain: AuditChain) -> None:
    """감사 조회는 대개 최근 사건을 본다. 오래된 것부터 주면 매번 끝까지 넘겨야 한다."""
    for i in range(5):
        await chain.append(_event(actor=f"user{i}"))
    found = chain.query(limit=2)
    assert [r.event.actor for r in found] == ["user4", "user3"]


async def test_기간으로_거른다(chain: AuditChain) -> None:
    await chain.append(_event())
    future = datetime.now(UTC) + timedelta(minutes=5)
    assert chain.query(since=future) == []
    assert len(chain.query(since=datetime.now(UTC) - timedelta(minutes=5))) == 1


# --- API ---------------------------------------------------------------


async def test_버스로_들어온_기록이_체인에_쌓인다(tmp_path: Path) -> None:
    """일반 경로. 저장소가 밀려도 상담은 계속 돈다.

    TestClient는 앱을 별도 스레드·이벤트 루프에서 돌리므로 인메모리 버스를
    양쪽에서 쓸 수 없다. 워커를 이 루프에서 직접 돌려 소비 경로를 검증한다.
    """
    import asyncio

    bus = InMemoryEventBus()
    store = AuditChain(tmp_path / "audit")
    store.load()
    worker = AuditWorker(bus, store, group="sec", consumer="sec-1")
    bus.register_group(Topic.AUDIT_LOG, "sec")

    task = asyncio.create_task(worker.run())
    try:
        await bus.publish(Topic.AUDIT_LOG, _event(actor="from-bus"))
        for _ in range(200):
            if store.count:
                break
            await asyncio.sleep(0.005)
    finally:
        worker.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert store.count == 1
    assert store.query()[0].event.actor == "from-bus"
    assert store.verify().intact


async def test_동기_기록은_디스크에_쓴_뒤_응답한다(tmp_path: Path) -> None:
    """PII 열람처럼 '기록 성공 후 허용'이 필요한 경로가 이 응답을 신뢰한다.
    201을 받았는데 파일에 없으면 그 열람은 감사에서 없었던 일이 된다."""
    store = AuditChain(tmp_path / "audit")
    app = create_app(bus=InMemoryEventBus(), chain=store)

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/audit",
            json=_event(actor="reveal-operator").model_dump(mode="json"),
        )
        assert response.status_code == 201
        # 응답 시점에 이미 세그먼트 파일에 있어야 한다.
        assert "reveal-operator" in store.segments()[0].read_text(encoding="utf-8")

        verified = client.get("/internal/v1/audit/verify").json()
        assert verified["intact"] and verified["total"] == 1


async def test_거부된_시도도_남는다(tmp_path: Path) -> None:
    """성공만 남기면 침해 시도가 감사에 보이지 않는다."""
    store = AuditChain(tmp_path / "audit")
    app = create_app(bus=InMemoryEventBus(), chain=store)
    denied = _event(action=AuditAction.ACCESS_DENIED)
    denied.outcome = AuditOutcome.DENIED

    with TestClient(app) as client:
        client.post("/internal/v1/audit", json=denied.model_dump(mode="json"))
        found = client.get(
            "/internal/v1/audit", params={"action": AuditAction.ACCESS_DENIED.value}
        ).json()

    assert len(found) == 1
    assert found[0]["event"]["outcome"] == "denied"


async def test_암호화가_꺼져_있으면_이유와_함께_드러난다(tmp_path: Path) -> None:
    """제안서에는 암호화가 있는데 현장에서는 꺼져 있는 상태가 이 영역의 대표 사고다."""
    app = create_app(bus=InMemoryEventBus(), chain=AuditChain(tmp_path / "audit"))
    with TestClient(app) as client:
        status = client.get("/internal/v1/crypto/status").json()
    assert status["enabled"] is False
    assert "VAI_SEC_MASTER_KEY_PATH" in status["reason"]
