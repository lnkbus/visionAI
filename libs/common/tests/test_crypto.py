"""저장 암호화 — 여기서 틀리면 고객사 데이터가 영구히 사라지거나 새어 나간다.

두 가지 실패를 특히 경계한다: ①마스터 키 덮어쓰기로 기존 데이터를 못 읽게 되는 것,
②암호화가 꺼진 줄 모르고 평문으로 저장되는 것.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from vai_common.crypto import (
    KEY_BYTES,
    AesGcmCipher,
    CryptoError,
    KcmvpCipher,
    SealedData,
    Vault,
    create_cipher,
    generate_key,
    load_master_key,
    write_master_key,
)


def test_봉인한_데이터를_되돌린다() -> None:
    vault = Vault(generate_key())
    sealed = vault.seal("계좌번호는 110-123-456789입니다".encode())
    assert vault.open(sealed).decode() == "계좌번호는 110-123-456789입니다"


def test_봉인문에_평문이_남지_않는다() -> None:
    vault = Vault(generate_key())
    sealed = vault.seal(b"secret-token-value")
    assert b"secret-token-value" not in sealed.ciphertext


def test_레코드마다_다른_DEK를_쓴다() -> None:
    """DEK를 재사용하면 키 하나가 새는 순간 전체가 열린다."""
    vault = Vault(generate_key())
    first, second = vault.seal(b"same"), vault.seal(b"same")
    assert first.wrapped_key != second.wrapped_key
    assert first.ciphertext != second.ciphertext  # nonce도 매번 다르다


def test_다른_마스터_키로는_열리지_않는다() -> None:
    sealed = Vault(generate_key()).seal(b"payload")
    with pytest.raises(CryptoError):
        Vault(generate_key()).open(sealed)


def test_본문이_한_바이트라도_바뀌면_거부한다() -> None:
    """AEAD를 쓰는 이유. 인증 태그가 없으면 '복호는 됐는데 내용이 바뀐'
    데이터가 감사에서 그대로 증거로 쓰인다."""
    vault = Vault(generate_key())
    sealed = vault.seal(b"amount=1000")
    broken = SealedData(
        ciphertext=bytes([sealed.ciphertext[0] ^ 0x01]) + sealed.ciphertext[1:],
        nonce=sealed.nonce,
        wrapped_key=sealed.wrapped_key,
        wrap_nonce=sealed.wrap_nonce,
        algorithm=sealed.algorithm,
        key_id=sealed.key_id,
    )
    with pytest.raises(CryptoError):
        vault.open(broken)


def test_AAD가_다르면_복호되지_않는다() -> None:
    """테넌트 ID를 AAD로 묶으면 다른 테넌트의 봉투를 가져와도 열리지 않는다."""
    vault = Vault(generate_key())
    sealed = vault.seal(b"tenant-a-data", aad=b"tenant-a")
    with pytest.raises(CryptoError):
        vault.open(sealed, aad=b"tenant-b")


def test_키_교체는_본문을_다시_쓰지_않는다() -> None:
    """수백 GB 녹취를 재암호화해야 하는 설계는 폐쇄망에서 사실상 교체 불가다."""
    old_key, new_key = generate_key(), generate_key()
    vault = Vault(old_key, key_id="k1")
    sealed = vault.seal("녹취 본문".encode())

    rewrapped = vault.rewrap(sealed, new_key, "k2")
    assert rewrapped.ciphertext == sealed.ciphertext  # 본문은 그대로
    assert rewrapped.key_id == "k2"
    assert Vault(new_key, key_id="k2").open(rewrapped).decode() == "녹취 본문"
    with pytest.raises(CryptoError):
        Vault(old_key, key_id="k1").open(rewrapped)  # 옛 키로는 못 연다


def test_직렬화를_왕복해도_열린다() -> None:
    """DB JSON 컬럼에 넣었다 꺼내는 실제 경로."""
    key = generate_key()
    sealed = Vault(key).seal(b"round-trip")
    assert Vault(key).open(SealedData.from_dict(sealed.to_dict())) == b"round-trip"


def test_알고리즘이_다르면_열지_않는다() -> None:
    """어댑터를 바꾼 뒤 옛 데이터를 새 알고리즘으로 읽으려 하면 조용히
    깨지는 대신 명시적으로 거부한다."""
    key = generate_key()
    sealed = Vault(key).seal(b"x")
    mismatched = SealedData(
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        wrapped_key=sealed.wrapped_key,
        wrap_nonce=sealed.wrap_nonce,
        algorithm="ARIA-256-GCM",
    )
    with pytest.raises(CryptoError, match="알고리즘"):
        Vault(key).open(mismatched)


def test_키_길이가_틀리면_거부한다() -> None:
    with pytest.raises(CryptoError, match="32"):
        Vault(b"short")


# --- 마스터 키 파일 -------------------------------------------------------


def test_마스터_키는_0600으로_만들어진다(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    write_master_key(path)
    assert path.stat().st_mode & 0o077 == 0
    assert len(load_master_key(path)) == KEY_BYTES


def test_이미_있는_마스터_키를_덮어쓰지_않는다(tmp_path: Path) -> None:
    """덮어쓰면 기존 데이터를 영구히 복호할 수 없다. 실수 한 번으로
    고객사의 전체 녹취가 사라진다."""
    path = tmp_path / "master.key"
    write_master_key(path)
    with pytest.raises(CryptoError, match="이미 있다"):
        write_master_key(path)


def test_권한이_느슨한_키_파일은_거부한다(tmp_path: Path) -> None:
    """그룹·타인이 읽을 수 있는 키는 저장 암호화를 무의미하게 만든다."""
    path = tmp_path / "loose.key"
    path.write_bytes(base64.b64encode(generate_key()))
    path.chmod(0o644)
    with pytest.raises(CryptoError, match="권한"):
        load_master_key(path)


def test_원시_바이트_키도_읽는다(tmp_path: Path) -> None:
    """운영자가 base64 대신 원시 32바이트로 보관하는 경우."""
    path = tmp_path / "raw.key"
    key = generate_key()
    path.write_bytes(key)
    path.chmod(0o600)
    assert load_master_key(path) == key


def test_키_파일이_없으면_이유를_알려_준다(tmp_path: Path) -> None:
    with pytest.raises(CryptoError, match="없다"):
        load_master_key(tmp_path / "absent.key")


# --- 어댑터 --------------------------------------------------------------


def test_어댑터_레지스트리() -> None:
    assert isinstance(create_cipher("aes-gcm"), AesGcmCipher)
    assert isinstance(create_cipher("kcmvp"), KcmvpCipher)
    with pytest.raises(ValueError, match="알 수 없는"):
        create_cipher("rot13")


def test_KCMVP는_제공자_없이_조용히_폴백하지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """검증모듈을 쓴다고 제안서에 적어 놓고 실제로는 소프트웨어 암호가
    도는 것이 이 영역에서 가장 나쁜 사고다."""
    monkeypatch.delenv("VAI_SEC_KCMVP_PROVIDER", raising=False)
    with pytest.raises(CryptoError, match="KCMVP 제공자"):
        KcmvpCipher().encrypt(generate_key(), b"x")


def test_KCMVP_제공자를_지정하면_그것을_쓴다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """구축 시 벤더 SDK를 붙이는 실제 경로. 모듈 경로 문자열만으로 연동되는지
    확인한다 — 여기가 안 되면 현장에서 검증모듈을 붙일 방법이 없다."""
    provider = tmp_path / "fake_kcmvp.py"
    provider.write_text(
        "from vai_common.crypto import AesGcmCipher\nPROVIDER = AesGcmCipher()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("VAI_SEC_KCMVP_PROVIDER", "fake_kcmvp:PROVIDER")

    key = generate_key()
    cipher = KcmvpCipher()
    ciphertext, nonce = cipher.encrypt(key, b"aria-payload")
    assert cipher.decrypt(key, ciphertext, nonce) == b"aria-payload"
