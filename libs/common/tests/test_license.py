"""오프라인 DRM — 위조·만료·복제의 경계.

이 파일이 지키는 것은 매출이다. 서명 검증이 뚫리면 라이선스는 장식이 되고,
반대로 만료 처리가 과하면 고객사 업무가 멈춰 다음 계약이 사라진다.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vai_common import fingerprint as fp
from vai_common.license import (
    GRACE_DAYS,
    BlockGrant,
    LicenseError,
    LicenseGate,
    LicenseSignatureError,
    canonical_payload,
    resolve_public_key,
    sign_payload,
    verify_signature,
)


@pytest.fixture(scope="module")
def key_pair() -> tuple[bytes, bytes]:
    """RSA-4096 생성은 수 초가 걸린다. 모듈 단위로 한 번만 만든다."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": 1,
        "customer_id": "ACME-BANK",
        "expires": (datetime.now(UTC).date() + timedelta(days=365)).isoformat(),
        "blocks": {
            "STT-CORE": {"enabled": True, "concurrent_channels": 50},
            "RAG-SRCH": {"enabled": True},
            "AVA-COUNSEL": {"enabled": False},
        },
    }
    base.update(overrides)
    return base


def _document(payload: dict[str, Any], private_pem: bytes) -> dict[str, Any]:
    return {"payload": payload, "signature": sign_payload(payload, private_pem)}


# --- 서명 ---------------------------------------------------------------


def test_정상_서명은_검증을_통과한다(key_pair: tuple[bytes, bytes]) -> None:
    private_pem, public_pem = key_pair
    payload = _payload()
    verify_signature(payload, sign_payload(payload, private_pem), public_pem)


def test_페이로드를_한_글자만_고쳐도_검증이_실패한다(key_pair: tuple[bytes, bytes]) -> None:
    """가장 흔한 위조 시도: 만료일이나 채널 수를 손으로 고치는 것."""
    private_pem, public_pem = key_pair
    payload = _payload()
    signature = sign_payload(payload, private_pem)

    tampered = _payload(expires="2099-12-31")
    with pytest.raises(LicenseSignatureError):
        verify_signature(tampered, signature, public_pem)


def test_다른_키로_만든_서명은_거부한다(key_pair: tuple[bytes, bytes]) -> None:
    """고객사가 스스로 키쌍을 만들어 서명하는 경우."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    _, public_pem = key_pair
    rogue = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    rogue_pem = rogue.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    payload = _payload()
    with pytest.raises(LicenseSignatureError):
        verify_signature(payload, sign_payload(payload, rogue_pem), public_pem)


def test_키_길이가_부족하면_서명하지_않는다() -> None:
    """RSA-2048로 발급하면 조달 보안 요구사항을 못 맞춘다."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    weak = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    weak_pem = weak.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(LicenseError, match="4096"):
        sign_payload(_payload(), weak_pem)


def test_정규화는_키_순서와_무관하게_같은_바이트를_만든다() -> None:
    """발급기와 검증기의 딕셔너리 순서가 다르면 서명이 깨진다."""
    assert canonical_payload({"a": 1, "b": 2}) == canonical_payload({"b": 2, "a": 1})


def test_한글_고객사명이_정규화에서_깨지지_않는다() -> None:
    """ensure_ascii=True로 바뀌면 이미 나간 라이선스가 전부 검증에 실패한다."""
    assert "주식회사" in canonical_payload({"customer_id": "주식회사 링버스"}).decode()


# --- 파일 적재 -----------------------------------------------------------


def test_서명이_없는_파일은_공개키가_있으면_거부한다(
    key_pair: tuple[bytes, bytes], tmp_path: Path
) -> None:
    _, public_pem = key_pair
    path = tmp_path / "no-sig.lic"
    path.write_text(json.dumps({"payload": _payload()}), encoding="utf-8")
    with pytest.raises(LicenseSignatureError):
        LicenseGate.load(str(path), public_key_pem=public_pem, verify_fingerprint=False)


def test_경로가_없으면_개발_모드로_전부_허용한다() -> None:
    gate = LicenseGate.load(None)
    assert gate.dev_mode
    assert gate.require("AVA-COUNSEL").enabled  # 개발 중에는 미출시 블록도 돈다


def test_허가된_블록만_기동한다(key_pair: tuple[bytes, bytes], tmp_path: Path) -> None:
    private_pem, public_pem = key_pair
    path = tmp_path / "acme.lic"
    path.write_text(json.dumps(_document(_payload(), private_pem)), encoding="utf-8")

    gate = LicenseGate.load(str(path), public_key_pem=public_pem, verify_fingerprint=False)
    assert gate.require("STT-CORE").limit("concurrent_channels") == 50
    with pytest.raises(LicenseError, match="AVA-COUNSEL"):
        gate.require("AVA-COUNSEL")  # enabled=false
    with pytest.raises(LicenseError, match="SPK-DIA"):
        gate.require("SPK-DIA")  # 라이선스에 아예 없다


def test_enabled_플래그가_용량으로_둔갑하지_않는다(
    key_pair: tuple[bytes, bytes], tmp_path: Path
) -> None:
    """파이썬에서 bool은 int의 하위형이다. 걸러 내지 않으면 ``enabled: true``가
    용량 1이 되어 동시 채널 1개짜리 라이선스가 된다."""
    private_pem, public_pem = key_pair
    path = tmp_path / "flag.lic"
    path.write_text(json.dumps(_document(_payload(), private_pem)), encoding="utf-8")

    gate = LicenseGate.load(str(path), public_key_pem=public_pem, verify_fingerprint=False)
    assert "enabled" not in gate.require("RAG-SRCH").capacity
    assert gate.status().capacity["STT-CORE"] == {"concurrent_channels": 50}


# --- 만료와 유예 ---------------------------------------------------------


def _gate_expiring(days_ago: int) -> LicenseGate:
    expired_on = datetime.now(UTC).date() - timedelta(days=days_ago)
    return LicenseGate({"STT-CORE": BlockGrant(True, {})}, expired_on, dev_mode=False)


def test_유예_기간_안에서는_계속_동작한다() -> None:
    """갱신이 며칠 늦었다고 상담을 끊으면 고객사 업무가 멈춘다."""
    gate = _gate_expiring(GRACE_DAYS - 1)
    assert gate.is_expired()
    assert gate.in_grace_period()
    assert gate.require("STT-CORE").enabled


def test_유예를_넘기면_기동을_거부한다() -> None:
    gate = _gate_expiring(GRACE_DAYS + 1)
    assert not gate.in_grace_period()
    with pytest.raises(LicenseError, match="만료"):
        gate.require("STT-CORE")


def test_만료_경계일에는_아직_만료가_아니다() -> None:
    """만료일 당일까지는 유효하다. 하루 일찍 끊으면 계약 위반이다."""
    gate = LicenseGate({}, datetime.now(UTC).date(), dev_mode=False)
    assert not gate.is_expired()
    assert gate.days_remaining() == 0


def test_무기한_라이선스는_만료되지_않는다() -> None:
    gate = LicenseGate({}, None, dev_mode=False)
    assert not gate.is_expired()
    assert gate.days_remaining() is None


# --- 지문 ---------------------------------------------------------------


def test_다른_서버의_지문이면_유예_없이_거부한다(
    key_pair: tuple[bytes, bytes], tmp_path: Path
) -> None:
    """복제 방지가 DRM의 존재 이유다. 만료와 달리 유예하지 않는다."""
    private_pem, public_pem = key_pair
    payload = _payload(
        fingerprint={"components": {"gpu": "aaa", "cpu": "bbb", "machine": "ccc", "mac": "ddd"}}
    )
    path = tmp_path / "other-server.lic"
    path.write_text(json.dumps(_document(payload, private_pem)), encoding="utf-8")

    with pytest.raises(LicenseError, match="지문"):
        LicenseGate.load(str(path), public_key_pem=public_pem, verify_fingerprint=True)


def test_이_서버의_지문이면_통과한다(key_pair: tuple[bytes, bytes], tmp_path: Path) -> None:
    private_pem, public_pem = key_pair
    payload = _payload(fingerprint={"components": dict(fp.collect().components)})
    path = tmp_path / "this-server.lic"
    path.write_text(json.dumps(_document(payload, private_pem)), encoding="utf-8")

    gate = LicenseGate.load(str(path), public_key_pem=public_pem, verify_fingerprint=True)
    assert gate.status().fingerprint_ok
    assert gate.require("STT-CORE").enabled


# --- 공개키 해석 ---------------------------------------------------------


def test_공개키_경로를_줬는데_없으면_기동을_막는다(tmp_path: Path) -> None:
    """배포 실수로 키가 빠졌을 때 조용히 검증을 끄면 서명 없는 라이선스가
    운영에 들어간다."""
    with pytest.raises(LicenseError, match="공개키"):
        resolve_public_key(str(tmp_path / "missing.pem"))


def test_공개키가_없으면_서명_검증을_건너뛴_것으로_표시한다(tmp_path: Path) -> None:
    """개발 경로. 이 상태로 납품되는 것을 화면에서 잡아내야 한다."""
    path = tmp_path / "unsigned.lic"
    path.write_text(json.dumps({"payload": _payload()}), encoding="utf-8")
    gate = LicenseGate.load(str(path), verify_fingerprint=False)
    assert not gate.status().signature_verified


def test_페이로드가_없는_파일은_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "broken.lic"
    path.write_text(json.dumps({"signature": "x"}), encoding="utf-8")
    with pytest.raises(LicenseError, match="payload"):
        LicenseGate.load(str(path), verify_fingerprint=False)


def test_status는_비활성_블록을_빼고_보고한다() -> None:
    gate = LicenseGate(
        {"STT-CORE": BlockGrant(True, {"concurrent_channels": 10}), "TTS": BlockGrant(False, {})},
        date(2099, 1, 1),
        dev_mode=False,
        customer_id="ACME",
    )
    status = gate.status()
    assert status.blocks == ["STT-CORE"]
    assert "TTS" not in status.capacity


# --- 파일 부재 -----------------------------------------------------------


def test_릴리스_빌드에서_라이선스_파일이_없으면_기동을_막는다(
    key_pair: tuple[bytes, bytes], tmp_path: Path
) -> None:
    """공개키가 내장된 빌드에서 파일 부재를 개발 모드로 취급하면
    '.lic을 지우면 무제한'이 되어 DRM 전체가 무의미해진다."""
    _, public_pem = key_pair
    with pytest.raises(LicenseError, match="라이선스 파일이 없다"):
        LicenseGate.load(str(tmp_path / "absent.lic"), public_key_pem=public_pem)


def test_개발_빌드에서는_파일이_없어도_기동한다(tmp_path: Path) -> None:
    """compose 데모처럼 공개키 없이 도는 구성. 여기서 죽으면 개발이 멈춘다."""
    assert LicenseGate.load(str(tmp_path / "absent.lic")).dev_mode


def test_잘린_파일은_원인이_보이는_오류로_바꾼다(tmp_path: Path) -> None:
    """전 블록이 기동하지 못하는 상황이다. FileNotFoundError나 JSONDecodeError
    스택트레이스만 남으면 현장에서 원인을 못 찾는다."""
    path = tmp_path / "truncated.lic"
    path.write_text('{"payload": {"customer', encoding="utf-8")
    with pytest.raises(LicenseError, match="읽을 수 없다"):
        LicenseGate.load(str(path))
