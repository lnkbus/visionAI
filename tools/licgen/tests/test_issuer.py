"""발급기 — 잘못 발급된 라이선스는 폐쇄망에서 며칠짜리 사고가 된다.

반입·반출에 승인 절차가 걸리므로, 발급 오류를 고객사 현장에서 발견하면
정정에 최소 며칠이 든다. 그래서 발급 시점에 최대한 막는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from licgen.cli import app as cli
from licgen.issuer import IssueError, catalog_block_ids, generate_key_pair, issue, parse_grant
from vai_common.license import LicenseGate, LicenseSignatureError, verify_signature
from vai_contracts.licensing import FingerprintReport, LicenseRequest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def key_pair() -> tuple[bytes, bytes]:
    return generate_key_pair(4096)


def _request(**overrides: object) -> LicenseRequest:
    data: dict[str, object] = {
        "customer_id": "ACME-BANK",
        "site": "본사",
        "fingerprint": FingerprintReport(
            combined="f" * 64, components={"gpu": "a" * 64, "cpu": "b" * 64}
        ),
        "requested_blocks": ["STT-CORE"],
    }
    data.update(overrides)
    return LicenseRequest.model_validate(data)


# --- 입력 파싱 -----------------------------------------------------------


def test_용량_없는_블록도_허가할_수_있다() -> None:
    assert parse_grant("RAG-SRCH") == ("RAG-SRCH", {})


def test_용량을_붙여_허가한다() -> None:
    assert parse_grant("stt-core:concurrent_channels=50") == (
        "STT-CORE",  # 대문자로 정규화 — 카탈로그 ID와 맞춘다
        {"concurrent_channels": 50},
    )


def test_형식이_틀리면_발급하지_않는다() -> None:
    with pytest.raises(IssueError):
        parse_grant("STT-CORE:concurrent_channels")


# --- 발급 ---------------------------------------------------------------


def test_발급한_라이선스가_제품_검증기를_통과한다(key_pair: tuple[bytes, bytes]) -> None:
    """발급기와 제품이 갈라지지 않는다는 것이 이 테스트의 전부다."""
    private_pem, public_pem = key_pair
    issued = issue(
        _request(),
        private_key_pem=private_pem,
        expires=date(2030, 1, 1),
        grants={"STT-CORE": {"concurrent_channels": 50}, "RAG-SRCH": {}},
        issued_on=date(2026, 1, 1),
    )
    gate = LicenseGate.from_document(
        issued.document, public_key_pem=public_pem, verify_fingerprint=False
    )
    assert gate.require("STT-CORE").limit("concurrent_channels") == 50
    assert gate.status().customer_id == "ACME-BANK"
    assert gate.status().signature_verified


def test_발급된_지문은_요청서와_동일하다(key_pair: tuple[bytes, bytes]) -> None:
    """발급기가 지문을 가공하면 설치 서버에서 검증이 깨진다."""
    private_pem, _ = key_pair
    request = _request()
    issued = issue(
        request,
        private_key_pem=private_pem,
        expires=None,
        grants={"STT-CORE": {}},
        issued_on=date(2026, 1, 1),
    )
    assert issued.payload["fingerprint"]["components"] == request.fingerprint.components


def test_지문_없는_요청서로는_발급하지_않는다(key_pair: tuple[bytes, bytes]) -> None:
    """어느 서버에서든 도는 라이선스는 DRM이 아니다."""
    private_pem, _ = key_pair
    request = _request(fingerprint=FingerprintReport(combined="", components={}))
    with pytest.raises(IssueError, match="지문"):
        issue(
            request,
            private_key_pem=private_pem,
            expires=None,
            grants={"STT-CORE": {}},
            issued_on=date(2026, 1, 1),
        )


def test_블록이_비면_발급하지_않는다(key_pair: tuple[bytes, bytes]) -> None:
    private_pem, _ = key_pair
    with pytest.raises(IssueError):
        issue(
            _request(),
            private_key_pem=private_pem,
            expires=None,
            grants={},
            issued_on=date(2026, 1, 1),
        )


def test_이미_지난_만료일로는_발급하지_않는다(key_pair: tuple[bytes, bytes]) -> None:
    """오타 한 번으로 기동하지 않는 라이선스가 나가는 것을 막는다."""
    private_pem, _ = key_pair
    with pytest.raises(IssueError, match="만료일"):
        issue(
            _request(),
            private_key_pem=private_pem,
            expires=date(2025, 1, 1),
            grants={"STT-CORE": {}},
            issued_on=date(2026, 1, 1),
        )


def test_다른_키의_공개키로는_검증되지_않는다(key_pair: tuple[bytes, bytes]) -> None:
    private_pem, _ = key_pair
    _, other_public = generate_key_pair(4096)
    issued = issue(
        _request(),
        private_key_pem=private_pem,
        expires=None,
        grants={"STT-CORE": {}},
        issued_on=date(2026, 1, 1),
    )
    with pytest.raises(LicenseSignatureError):
        verify_signature(issued.payload, issued.document["signature"], other_public)


def test_카탈로그에서_블록_ID를_읽는다() -> None:
    """발급 시 오타 검증의 근거. 카탈로그가 곧 허가 가능한 블록의 목록이다."""
    ids = catalog_block_ids(REPO_ROOT)
    assert {"CORE-LIC", "STT-CORE", "RAG-SRCH"} <= ids


# --- CLI ---------------------------------------------------------------


def test_CLI로_키생성부터_검사까지_돈다(tmp_path: Path) -> None:
    runner = CliRunner()
    keys = tmp_path / "keys"

    assert runner.invoke(cli, ["keygen", "--out-dir", str(keys)]).exit_code == 0
    # 개인키가 다른 사용자에게 읽히면 라이선스 체계 전체가 무너진다.
    assert (keys / "license_key.pem").stat().st_mode & 0o077 == 0

    # 이미 있는 키를 실수로 덮어쓰면 이미 나간 라이선스를 재발급할 수 없다.
    assert runner.invoke(cli, ["keygen", "--out-dir", str(keys)]).exit_code == 1

    request_file = tmp_path / "acme.req"
    request_file.write_text(_request().model_dump_json(), encoding="utf-8")
    out = tmp_path / "acme.lic"

    issued = runner.invoke(
        cli,
        [
            "issue",
            str(request_file),
            "--key",
            str(keys / "license_key.pem"),
            "--out",
            str(out),
            "--expires",
            "2030-12-31",
            "--block",
            "STT-CORE:concurrent_channels=50",
            "--from-request",
            "--catalog-root",
            str(REPO_ROOT),
        ],
    )
    assert issued.exit_code == 0, issued.output
    assert out.exists()

    inspected = runner.invoke(
        cli, ["inspect", str(out), "--public-key", str(keys / "license_pub.pem")]
    )
    assert inspected.exit_code == 0
    assert "ACME-BANK" in inspected.output
    assert "concurrent_channels=50" in inspected.output


def test_CLI가_카탈로그에_없는_블록을_막는다(tmp_path: Path) -> None:
    """오타 하나로 고객사의 블록이 기동하지 못한다."""
    runner = CliRunner()
    keys = tmp_path / "keys"
    runner.invoke(cli, ["keygen", "--out-dir", str(keys)])

    request_file = tmp_path / "acme.req"
    request_file.write_text(_request().model_dump_json(), encoding="utf-8")

    result = runner.invoke(
        cli,
        [
            "issue",
            str(request_file),
            "--key",
            str(keys / "license_key.pem"),
            "--out",
            str(tmp_path / "acme.lic"),
            "--block",
            "STT-KORE",  # 오타
            "--catalog-root",
            str(REPO_ROOT),
        ],
    )
    assert result.exit_code == 1
    assert not (tmp_path / "acme.lic").exists()
