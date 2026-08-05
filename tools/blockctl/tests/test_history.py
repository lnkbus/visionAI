"""릴리스 이력이 이력으로 성립하는가."""

from __future__ import annotations

import pytest

from blockctl.history import (
    HistoryError,
    ReleaseEntry,
    append,
    load_history,
    render,
    verify,
    version_key,
)

HASH = "a" * 64
COMMIT = "b" * 40


def entry(version: str, date: str = "2026-08-05", **kw: object) -> ReleaseEntry:
    return ReleaseEntry(
        version=version,
        released_at=date,
        commit=str(kw.get("commit", COMMIT)),
        packages={"meeting": HASH},
    )


def test_clean_history_passes() -> None:
    assert verify([entry("0.1.0", "2026-01-01"), entry("0.2.0", "2026-03-01")]) == []


def test_duplicate_version_is_rejected() -> None:
    """같은 이름의 서로 다른 물건이 생기는 것이 형상 관리가 막으려는 상황이다."""
    problems = verify([entry("0.1.0", "2026-01-01"), entry("0.1.0", "2026-02-01")])
    assert any("중복" in p for p in problems)


def test_version_going_backwards_is_rejected() -> None:
    problems = verify([entry("0.2.0", "2026-01-01"), entry("0.1.0", "2026-02-01")])
    assert any("뒤로 갔다" in p for p in problems)


def test_date_going_backwards_is_rejected() -> None:
    problems = verify([entry("0.1.0", "2026-03-01"), entry("0.2.0", "2026-01-01")])
    assert any("이르다" in p for p in problems)


@pytest.mark.parametrize("bad", ["0.1", "v0.1.0", "0.1.0.1", "최신"])
def test_version_format_is_enforced(bad: str) -> None:
    assert any("버전 형식" in p for p in verify([entry(bad)]))


def test_short_commit_is_rejected() -> None:
    """짧은 해시는 언젠가 충돌한다 — 되돌아갈 좌표가 흔들리면 이력이 아니다."""
    assert any("40자리" in p for p in verify([entry("0.1.0", commit="abc1234")]))


def test_empty_packages_is_rejected() -> None:
    bad = ReleaseEntry(version="0.1.0", released_at="2026-08-05", commit=COMMIT, packages={})
    assert any("납품본과 대조" in p for p in verify([bad]))


def test_bad_configuration_hash_is_rejected() -> None:
    bad = ReleaseEntry(
        version="0.1.0", released_at="2026-08-05", commit=COMMIT, packages={"meeting": "짧음"}
    )
    assert any("64자리" in p for p in verify([bad]))


def test_append_refuses_to_break_the_history() -> None:
    """깨진 항목을 붙이고 나서 검사하면 이미 파일에 들어간 뒤다."""
    existing = [entry("0.2.0", "2026-03-01")]
    with pytest.raises(HistoryError, match="뒤로 갔다"):
        append(existing, entry("0.1.0", "2026-04-01"))
    assert len(existing) == 1, "실패한 추가가 원본을 건드리면 안 된다"


def test_prerelease_sorts_before_the_release() -> None:
    assert version_key("0.3.0-rc.1") < version_key("0.3.0")
    assert verify([entry("0.3.0-rc.1", "2026-01-01"), entry("0.3.0", "2026-02-01")]) == []


def test_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    entries = [
        ReleaseEntry(
            version="0.1.0",
            released_at="2026-01-01",
            commit=COMMIT,
            packages={"meeting": HASH, "aicc": "c" * 64},
            blocks={"STT-CORE": "0.2.0"},
            note="첫 납품",
        )
    ]
    path = tmp_path / "history.yaml"
    path.write_text(render(entries), encoding="utf-8")
    assert load_history(path) == entries


def test_missing_file_is_an_empty_history(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert load_history(tmp_path / "없음.yaml") == []


def test_broken_entry_raises_instead_of_being_skipped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """조용히 건너뛰면 이력에 구멍이 생기고 아무도 모른다."""
    path = tmp_path / "history.yaml"
    path.write_text("releases:\n  - version: 0.1.0\n", encoding="utf-8")
    with pytest.raises(HistoryError, match="commit"):
        load_history(path)
