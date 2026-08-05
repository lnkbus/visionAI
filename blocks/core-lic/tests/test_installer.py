"""라이선스 설치기 — 잘못 설치되면 고객사 전체가 기동하지 못한다.

특히 원자적 쓰기를 검증한다. 잘린 JSON이 남으면 그 순간부터 **모든 블록이**
기동에 실패하고, 폐쇄망에서는 원격으로 손볼 수도 없다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vai_common import fingerprint as fp
from vai_common.license import LicenseError, sign_payload
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_core_lic.app import create_app
from vai_core_lic.installer import LicenseInstaller, build_request, current_fingerprint


@pytest.fixture(scope="module")
def key_pair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    return (
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def _document(private_pem: bytes, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": 1,
        "customer_id": "ACME-BANK",
        "expires": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
        "fingerprint": {"components": dict(fp.collect().components)},
        "blocks": {"STT-CORE": {"enabled": True, "concurrent_channels": 30}},
    }
    payload.update(overrides)
    return {"payload": payload, "signature": sign_payload(payload, private_pem)}


# --- 지문·요청서 ---------------------------------------------------------


def test_지문_보고서에는_해시만_담긴다() -> None:
    """원본 MAC·GPU UUID가 담기면 발급요청서가 고객사 자산정보가 되어
    반출 승인 절차가 무거워진다."""
    report = current_fingerprint(include_gpu=False)
    assert report.combined
    assert all(len(value) == 64 for value in report.components.values())


def test_요청서에_지문이_실린다() -> None:
    request = build_request("ACME-BANK", site="본사", requested_blocks=["STT-CORE", "CORE-BUS"])
    assert request.fingerprint.components
    assert request.requested_blocks == ["CORE-BUS", "STT-CORE"]  # 정렬해 발급 시 비교가 쉽다


# --- 설치 ---------------------------------------------------------------


def test_정상_라이선스를_설치하면_파일이_생긴다(
    key_pair: tuple[bytes, bytes], tmp_path: Path
) -> None:
    private_pem, public_pem = key_pair
    target = tmp_path / "license" / "acme.lic"
    installer = LicenseInstaller(str(target), public_key_pem=public_pem)

    view = installer.install(_document(private_pem))
    assert view.installed and view.signature_verified and view.fingerprint_ok
    assert view.blocks == ["STT-CORE"]
    assert view.capacity["STT-CORE"]["concurrent_channels"] == 30
    assert json.loads(target.read_text(encoding="utf-8"))["payload"]["customer_id"] == "ACME-BANK"


def test_위조된_라이선스는_기존_파일을_건드리지_않는다(
    key_pair: tuple[bytes, bytes], tmp_path: Path
) -> None:
    """설치 실패가 기존 라이선스를 망가뜨리면, 갱신 시도가 곧 장애가 된다."""
    private_pem, public_pem = key_pair
    target = tmp_path / "acme.lic"
    installer = LicenseInstaller(str(target), public_key_pem=public_pem)
    installer.install(_document(private_pem))
    before = target.read_text(encoding="utf-8")

    forged = _document(private_pem)
    forged["payload"]["expires"] = "2099-12-31"  # 서명은 그대로 두고 만료만 고친다
    with pytest.raises(LicenseError):
        installer.install(forged)
    assert target.read_text(encoding="utf-8") == before


def test_검증만_요청하면_파일을_쓰지_않는다(key_pair: tuple[bytes, bytes], tmp_path: Path) -> None:
    private_pem, public_pem = key_pair
    target = tmp_path / "acme.lic"
    installer = LicenseInstaller(str(target), public_key_pem=public_pem)

    view = installer.verify(_document(private_pem))
    assert view.signature_verified
    assert not view.installed
    assert not target.exists()


def test_다른_서버의_지문이면_설치를_거부한다(
    key_pair: tuple[bytes, bytes], tmp_path: Path
) -> None:
    private_pem, public_pem = key_pair
    installer = LicenseInstaller(str(tmp_path / "acme.lic"), public_key_pem=public_pem)
    other = _document(
        private_pem, fingerprint={"components": {"gpu": "a" * 64, "cpu": "b" * 64, "mac": "c" * 64}}
    )
    with pytest.raises(LicenseError, match="지문"):
        installer.install(other)


def test_경로가_없으면_설치할_수_없다(key_pair: tuple[bytes, bytes]) -> None:
    private_pem, public_pem = key_pair
    installer = LicenseInstaller(None, public_key_pem=public_pem)
    with pytest.raises(LicenseError, match="VAI_LICENSE_PATH"):
        installer.install(_document(private_pem))


def test_설치_후_임시_파일이_남지_않는다(key_pair: tuple[bytes, bytes], tmp_path: Path) -> None:
    """원자적 쓰기의 흔적이 남으면 디렉토리가 지저분해지고, 백업 스크립트가
    잘린 파일을 진짜 라이선스로 착각한다."""
    private_pem, public_pem = key_pair
    target = tmp_path / "acme.lic"
    LicenseInstaller(str(target), public_key_pem=public_pem).install(_document(private_pem))
    assert [p.name for p in tmp_path.iterdir()] == ["acme.lic"]


def test_깨진_파일이_있어도_현황_조회는_죽지_않는다(tmp_path: Path) -> None:
    """운영자가 '왜 안 되는지' 보러 오는 화면이다. 여기서 500이 나면
    아무 단서도 못 얻는다."""
    target = tmp_path / "broken.lic"
    target.write_text("{ 잘린 JSON", encoding="utf-8")
    view = LicenseInstaller(str(target)).current()
    assert view.installed and not view.signature_verified


def test_설치_전에는_미설치로_보고한다(tmp_path: Path) -> None:
    assert not LicenseInstaller(str(tmp_path / "none.lic")).current().installed


# --- API ---------------------------------------------------------------


def test_API로_요청서를_만들고_설치까지_돈다(key_pair: tuple[bytes, bytes], tmp_path: Path) -> None:
    private_pem, public_pem = key_pair
    target = tmp_path / "acme.lic"
    app = create_app(LicenseInstaller(str(target), public_key_pem=public_pem))

    with TestClient(app) as client:
        assert client.get("/internal/v1/license").json()["installed"] is False

        created = client.post(
            "/internal/v1/license/request",
            json={"customer_id": "ACME-BANK", "site": "본사", "include_gpu": False},
        )
        assert created.status_code == 200
        assert created.json()["fingerprint"]["components"]

        installed = client.post(
            "/internal/v1/license", json={"content": _document(private_pem), "activate": True}
        )
        assert installed.status_code == 200
        assert installed.json()["blocks"] == ["STT-CORE"]

        current = client.get("/internal/v1/license").json()
        assert current["installed"] and current["customer_id"] == "ACME-BANK"


def test_위조_라이선스_설치는_400을_돌려준다(key_pair: tuple[bytes, bytes], tmp_path: Path) -> None:
    """500이면 운영자가 서버 장애로 오해하고 공급사에 장애 접수를 한다.
    파일이 잘못됐다는 사실이 응답에서 바로 보여야 한다."""
    private_pem, public_pem = key_pair
    app = create_app(LicenseInstaller(str(tmp_path / "acme.lic"), public_key_pem=public_pem))
    forged = _document(private_pem)
    forged["payload"]["blocks"]["AVA-COUNSEL"] = {"enabled": True}

    with TestClient(app) as client:
        response = client.post("/internal/v1/license", json={"content": forged, "activate": True})
    assert response.status_code == 400
    assert "서명" in response.json()["detail"]


def test_지문_엔드포인트가_해시를_돌려준다(tmp_path: Path) -> None:
    app = create_app(LicenseInstaller(str(tmp_path / "acme.lic")))
    with TestClient(app) as client:
        body = client.get("/internal/v1/license/fingerprint", params={"include_gpu": False}).json()
    assert len(body["combined"]) == 64


def test_라이선스_없이도_설치_창구는_뜬다(
    key_pair: tuple[bytes, bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """신규 구축의 최초 설치 경로. 설치기가 라이선스를 요구하면 아무도
    라이선스를 넣을 수 없다 — 닭과 달걀."""
    private_pem, public_pem = key_pair
    target = tmp_path / "acme.lic"
    keyfile = tmp_path / "license_pub.pem"
    keyfile.write_bytes(public_pem)

    monkeypatch.setenv("VAI_LICENSE_PATH", str(target))
    monkeypatch.setenv("VAI_LICENSE_PUBLIC_KEY_PATH", str(keyfile))
    get_settings.cache_clear()
    try:
        app = create_app()  # 라이선스 파일이 아직 없는 상태
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/internal/v1/license").json()["installed"] is False
            installed = client.post(
                "/internal/v1/license", json={"content": _document(private_pem)}
            )
        assert installed.status_code == 200
        assert target.exists()
    finally:
        get_settings.cache_clear()


def test_라이선스에_없는_블록은_기동하지_못한다(
    key_pair: tuple[bytes, bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """게이팅이 실제로 무는지 확인한다. CORE-LIC만 예외이고 나머지는 막혀야 한다."""
    private_pem, public_pem = key_pair
    target = tmp_path / "acme.lic"
    keyfile = tmp_path / "license_pub.pem"
    keyfile.write_bytes(public_pem)
    target.write_text(json.dumps(_document(private_pem)), encoding="utf-8")  # STT-CORE만 허가

    monkeypatch.setenv("VAI_LICENSE_PATH", str(target))
    monkeypatch.setenv("VAI_LICENSE_PUBLIC_KEY_PATH", str(keyfile))
    get_settings.cache_clear()
    try:
        with pytest.raises(LicenseError, match="FLT-MICRO"):
            create_block_app(block_id="FLT-MICRO", title="t")
        # 같은 라이선스로 CORE-LIC은 뜬다 — 설치 창구는 항상 살아 있어야 한다.
        assert create_block_app(block_id="CORE-LIC", title="t", license_optional=True)
    finally:
        get_settings.cache_clear()
