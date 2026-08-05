"""파일 요약 저장소 테스트.

회의록은 그 자체가 산출물이다. **재기동으로, TTL로, 프로세스 교체로 사라지면
그건 제품 결함이다.** 동시에 지울 수 있어야 한다 — 개인정보 파기 요구는
거절할 수 없다. 이 두 요구가 여기서 만난다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from vai_contracts.session import SessionProfile
from vai_contracts.summary import MeetingSummary, Summary, SummaryStatus
from vai_llm_sum.filestore import FileSummaryStore


def _files(root: Path, pattern: str = "*.json") -> list[Path]:
    """디스크 상태 확인용 동기 헬퍼.

    async 테스트 안에서 직접 훑으면 린터가 이벤트 루프 블로킹으로 본다 —
    실제로 그 규칙이 막으려는 것은 프로덕션 경로이므로, 동기 함수로 분리해
    규칙의 의미를 남긴다.
    """
    return list(root.rglob(pattern))


def _summary(session_id: str, tenant: str = "t1", *, days_ago: int = 0) -> Summary:
    return Summary(
        session_id=session_id,
        tenant_id=tenant,
        profile=SessionProfile.MEETING,
        status=SummaryStatus.READY,
        meeting=MeetingSummary(title=f"{session_id} 회의"),
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


async def test_summary_survives_a_new_store_instance(tmp_path: Path) -> None:
    """프로세스가 죽어도 남아야 한다 — 이것이 파일 저장소를 둔 이유다."""
    await FileSummaryStore(tmp_path).put(_summary("s1"))

    restarted = FileSummaryStore(tmp_path)
    found = await restarted.get("s1")

    assert found is not None
    assert found.meeting.title == "s1 회의"


async def test_list_is_newest_first(tmp_path: Path) -> None:
    store = FileSummaryStore(tmp_path)
    for name, age in [("old", 10), ("mid", 5), ("new", 0)]:
        await store.put(_summary(name, days_ago=age))

    listed = await store.list_for("t1")

    assert [s.session_id for s in listed] == ["new", "mid", "old"]


async def test_tenants_are_isolated(tmp_path: Path) -> None:
    store = FileSummaryStore(tmp_path)
    await store.put(_summary("a", "tenant-a"))
    await store.put(_summary("b", "tenant-b"))

    assert [s.session_id for s in await store.list_for("tenant-a")] == ["a"]
    assert [s.session_id for s in await store.list_for("tenant-b")] == ["b"]


async def test_update_overwrites_without_duplicating(tmp_path: Path) -> None:
    store = FileSummaryStore(tmp_path)
    original = _summary("s1")
    await store.put(original)

    edited = original.model_copy(update={"edited": True})
    edited.meeting.title = "수정된 제목"
    await store.put(edited)

    listed = await store.list_for("t1")
    assert len(listed) == 1
    assert listed[0].meeting.title == "수정된 제목"


async def test_delete_removes_the_file(tmp_path: Path) -> None:
    """파기 요구는 거절할 수 없다. 파일이 실제로 사라져야 한다."""
    store = FileSummaryStore(tmp_path)
    await store.put(_summary("s1"))

    assert await store.delete("s1") is True
    assert await store.get("s1") is None
    assert not _files(tmp_path, "s1.json")


async def test_delete_missing_reports_false(tmp_path: Path) -> None:
    assert await FileSummaryStore(tmp_path).delete("없는세션") is False


async def test_retention_purges_only_expired(tmp_path: Path) -> None:
    store = FileSummaryStore(tmp_path, retention_days=7)
    await store.put(_summary("keep", days_ago=1))
    await store.put(_summary("drop", days_ago=30))

    removed = await store.purge_expired()

    assert removed == 1
    assert await store.get("keep") is not None
    assert await store.get("drop") is None


async def test_retention_zero_keeps_everything(tmp_path: Path) -> None:
    """보관기간을 정하지 않았는데 제품이 임의로 지우면 그건 데이터 소실 사고다."""
    store = FileSummaryStore(tmp_path, retention_days=0)
    await store.put(_summary("ancient", days_ago=3650))

    assert await store.purge_expired() == 0
    assert await store.get("ancient") is not None


async def test_path_traversal_is_neutralised(tmp_path: Path) -> None:
    """세션 ID는 바깥에서 온다. 저장소 밖에 파일을 쓰게 두면 안 된다."""
    store = FileSummaryStore(tmp_path)
    await store.put(_summary("../../etc/passwd", tenant="../evil"))

    written = _files(tmp_path)
    assert written, "저장은 되어야 한다"
    for path in written:
        assert tmp_path in path.parents


async def test_corrupt_file_does_not_break_the_listing(tmp_path: Path) -> None:
    """한 건이 깨졌다고 목록 전체가 실패하면 남은 회의록도 못 본다."""
    store = FileSummaryStore(tmp_path)
    await store.put(_summary("good"))
    await store.put(_summary("bad"))
    broken = _files(tmp_path, "bad.json")[0]
    broken.write_text("{ 깨진 JSON", encoding="utf-8")

    listed = await store.list_for("t1")

    assert [s.session_id for s in listed] == ["good"]


async def test_index_is_rebuilt_when_lost(tmp_path: Path) -> None:
    """인덱스는 파생 데이터다. 잃어버려도 원본에서 다시 만들어야 한다."""
    store = FileSummaryStore(tmp_path)
    await store.put(_summary("s1"))
    for index in _files(tmp_path, ".index.json"):
        index.write_text("깨짐", encoding="utf-8")

    assert await FileSummaryStore(tmp_path).get("s1") is not None


async def test_no_partial_file_on_write(tmp_path: Path) -> None:
    """부분 기록된 JSON이 남으면 이후 조회가 통째로 실패한다."""
    store = FileSummaryStore(tmp_path)
    await store.put(_summary("s1"))

    leftovers = [p for p in _files(tmp_path, "*") if p.is_file() and p.suffix != ".json"]
    assert not leftovers, f"임시 파일이 남았다: {leftovers}"
