"""권한을 조이지 못해도 감사는 남는가.

감사 디렉토리는 소유자만 봐야 한다 — 열려 있으면 그 자체가 유출 경로다.
그런데 마운트된 볼륨의 소유자가 다르면 `chmod` 가 거부된다(맥의 Docker
Desktop 이 그렇다). 거기서 죽으면 **감사 블록이 통째로 안 뜨고, 감사 로그가
아예 안 남는다.**

권한이 넓은 것과 기록이 없는 것 중에서는 **기록이 없는 쪽이 나쁘다.** 그래서
계속 가되, 조용히 넘어가지는 않는다 — 보안 태세 문제라 로그에 남아야 하고,
운영자가 호스트 쪽에서 잡을 수 있어야 한다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vai_core_sec.chain import AuditChain


def test_권한을_못_조여도_기동한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", denied)
    chain = AuditChain(tmp_path / "audit")

    with caplog.at_level("WARNING"):
        chain.load()

    assert chain.count == 0, "빈 디렉토리에서 시작한다"
    assert any("권한" in record.message for record in caplog.records), (
        "조용히 넘어가면 안 된다 — 보안 태세 문제다"
    )


def test_조일_수_있으면_조인다(tmp_path: Path) -> None:
    target = tmp_path / "audit"
    chain = AuditChain(target)

    chain.load()

    assert target.is_dir()
    assert target.stat().st_mode & 0o777 == 0o700
