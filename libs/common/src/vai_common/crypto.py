"""저장 데이터 암호화 — 사양서 §5의 KCMVP 요구에 대응한다.

**왜 공통 라이브러리인가.** 암호화는 데이터를 저장하는 모든 블록이 쓴다.
CORE-SEC의 HTTP API로 제공하면 저장 경로마다 네트워크 왕복이 붙고, CORE-SEC이
죽는 순간 아무 블록도 데이터를 쓰지 못한다. 암호 프리미티브는 인프로세스로 두고,
**키 관리와 감사**만 CORE-SEC이 맡는다.

**봉투 암호화(envelope)를 쓴다.** 레코드마다 임의의 DEK로 본문을 암호화하고,
DEK를 마스터 키(KEK)로 감싸 함께 저장한다. 키 교체 시 DEK만 다시 감싸면 되므로
수백 GB의 녹취를 재암호화하지 않아도 된다 — 폐쇄망에서 전체 재암호화는
서비스 중단을 뜻한다.

**KCMVP 검증모듈은 고객사 조달품이다.** ARIA/SEED 검증모듈은 벤더마다 연동
방식(PKCS#11, 전용 SDK, HSM)이 달라 하나를 가정해 만들면 대부분의 현장에서
못 쓴다. 그래서 :class:`BaseCipher` 어댑터로 두고, 기본은 소프트웨어
AES-256-GCM을 쓴다. 검증모듈 요구가 있는 사업에서는 구축 시 어댑터를 붙인다.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEY_BYTES = 32
"""AES-256 / ARIA-256 공통. 마스터 키 파일도 이 길이를 요구한다."""

NONCE_BYTES = 12
"""GCM 표준 nonce 길이. 96비트가 아닌 값은 내부 변환이 붙어 상호운용이 나빠진다."""


class CryptoError(RuntimeError):
    """암복호 실패. 키가 틀렸거나 데이터가 변조됐다."""


@dataclass(frozen=True)
class SealedData:
    """봉투 암호화 결과. 이 구조 그대로 저장하면 복호에 필요한 것이 다 들어 있다."""

    ciphertext: bytes
    nonce: bytes
    wrapped_key: bytes
    """KEK로 감싼 DEK. 마스터 키 없이는 풀 수 없다."""

    wrap_nonce: bytes
    algorithm: str
    key_id: str = ""
    """어떤 마스터 키로 감쌌는지. 교체 이력이 있는 저장소에서 필수다."""

    def to_dict(self) -> dict[str, str]:
        """JSON 컬럼·파일에 그대로 넣을 수 있는 형태."""
        return {
            "ciphertext": base64.b64encode(self.ciphertext).decode(),
            "nonce": base64.b64encode(self.nonce).decode(),
            "wrapped_key": base64.b64encode(self.wrapped_key).decode(),
            "wrap_nonce": base64.b64encode(self.wrap_nonce).decode(),
            "algorithm": self.algorithm,
            "key_id": self.key_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> SealedData:
        try:
            return cls(
                ciphertext=base64.b64decode(raw["ciphertext"]),
                nonce=base64.b64decode(raw["nonce"]),
                wrapped_key=base64.b64decode(raw["wrapped_key"]),
                wrap_nonce=base64.b64decode(raw["wrap_nonce"]),
                algorithm=raw["algorithm"],
                key_id=raw.get("key_id", ""),
            )
        except (KeyError, ValueError) as exc:
            raise CryptoError(f"봉투 형식이 올바르지 않다: {exc}") from exc


class BaseCipher(ABC):
    """대칭 암복호 어댑터.

    AEAD(인증 암호)만 허용한다. 인증 태그가 없으면 변조를 탐지하지 못하고,
    "복호는 됐는데 내용이 바뀐" 데이터가 감사에서 그대로 증거로 쓰인다.
    """

    name: str
    algorithm: str

    @abstractmethod
    def encrypt(self, key: bytes, plaintext: bytes, *, aad: bytes = b"") -> tuple[bytes, bytes]:
        """``(ciphertext, nonce)``를 돌려준다."""

    @abstractmethod
    def decrypt(
        self, key: bytes, ciphertext: bytes, nonce: bytes, *, aad: bytes = b""
    ) -> bytes: ...


class AesGcmCipher(BaseCipher):
    """AES-256-GCM. 검증모듈 요구가 없는 사업의 기본값."""

    name = "aes-gcm"
    algorithm = "AES-256-GCM"

    def encrypt(self, key: bytes, plaintext: bytes, *, aad: bytes = b"") -> tuple[bytes, bytes]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        _require_key(key)
        nonce = secrets.token_bytes(NONCE_BYTES)
        return AESGCM(key).encrypt(nonce, plaintext, aad or None), nonce

    def decrypt(self, key: bytes, ciphertext: bytes, nonce: bytes, *, aad: bytes = b"") -> bytes:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        _require_key(key)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad or None)
        except InvalidTag as exc:
            # 키가 틀렸거나 데이터가 변조됐다. 둘을 구분해 알려 주면
            # 공격자에게 정보를 주므로 한 가지 메시지로 묶는다.
            raise CryptoError("복호에 실패했다 (키 불일치 또는 변조)") from exc


class KcmvpCipher(BaseCipher):
    """KCMVP 검증모듈 연동 지점 (ARIA-256-GCM).

    실제 모듈은 고객사가 조달하며 연동 방식이 벤더마다 다르다. 구축 시
    ``VAI_SEC_KCMVP_PROVIDER``에 제공자 모듈 경로를 지정하면 이 어댑터가
    그것을 불러 쓴다. 제공자는 :class:`BaseCipher`와 같은 시그니처의
    ``encrypt``/``decrypt``를 노출해야 한다.

    **지정 없이 조용히 AES로 폴백하지 않는다.** 검증모듈을 쓴다고 제안서에
    적어 놓고 실제로는 소프트웨어 암호가 도는 것이 이 영역에서 가장 나쁜 사고다.
    """

    name = "kcmvp"
    algorithm = "ARIA-256-GCM"

    def __init__(self) -> None:
        self._provider: Any = None

    def _load(self) -> Any:
        if self._provider is not None:
            return self._provider
        target = os.environ.get("VAI_SEC_KCMVP_PROVIDER", "")
        if not target:
            raise CryptoError(
                "KCMVP 제공자가 지정되지 않았다 (VAI_SEC_KCMVP_PROVIDER). "
                "검증모듈 연동 없이 이 어댑터를 쓸 수 없다"
            )
        module_name, _, attr = target.partition(":")
        import importlib

        module = importlib.import_module(module_name)
        self._provider = getattr(module, attr) if attr else module
        log.info("KCMVP 제공자 로드", extra={"provider": target})
        return self._provider

    def encrypt(self, key: bytes, plaintext: bytes, *, aad: bytes = b"") -> tuple[bytes, bytes]:
        result: tuple[bytes, bytes] = self._load().encrypt(key, plaintext, aad=aad)
        return result

    def decrypt(self, key: bytes, ciphertext: bytes, nonce: bytes, *, aad: bytes = b"") -> bytes:
        result: bytes = self._load().decrypt(key, ciphertext, nonce, aad=aad)
        return result


_REGISTRY: dict[str, Callable[[], BaseCipher]] = {
    "aes-gcm": AesGcmCipher,
    "kcmvp": KcmvpCipher,
}


def create_cipher(name: str) -> BaseCipher:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 암호 어댑터 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None


def _require_key(key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise CryptoError(f"키 길이는 {KEY_BYTES}바이트여야 한다 (현재 {len(key)})")


def generate_key() -> bytes:
    """새 마스터 키. ``secrets``는 OS CSPRNG를 쓴다."""
    return secrets.token_bytes(KEY_BYTES)


def load_master_key(path: str | Path) -> bytes:
    """마스터 키 파일을 읽는다.

    파일 권한을 확인한다. 그룹·타인이 읽을 수 있는 키 파일은 저장 암호화를
    무의미하게 만들고, 보안 심사에서 즉시 지적된다.
    """
    file = Path(path)
    if not file.is_file():
        raise CryptoError(f"마스터 키 파일이 없다: {path}")

    mode = file.stat().st_mode
    if mode & 0o077:
        raise CryptoError(
            f"마스터 키 파일 권한이 느슨하다 (chmod 600 필요): {path} ({mode & 0o777:o})"
        )

    raw = file.read_bytes()
    # 운영자가 base64로 보관하는 경우가 흔하다. 둘 다 받는다.
    #
    # **원시 바이트를 먼저 본다.** 무작위 32바이트 중 약 5%는 공백 바이트(0x20,
    # 0x0a 등)로 시작하거나 끝난다. strip부터 하면 그런 키가 조용히 잘려
    # 길이가 안 맞고, 결국 "가끔 복호가 안 되는" 설치가 된다.
    if len(raw) == KEY_BYTES:
        return raw

    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except ValueError as exc:
        raise CryptoError(
            f"마스터 키를 해석할 수 없다: {path} (원시 {KEY_BYTES}바이트 또는 base64)"
        ) from exc
    _require_key(key)
    return key


def write_master_key(path: str | Path, key: bytes | None = None) -> bytes:
    """마스터 키를 0600으로 만든다. 이미 있으면 덮어쓰지 않는다.

    덮어쓰면 **기존 데이터를 영구히 복호할 수 없다.** 실수 한 번으로 고객사의
    전체 녹취가 사라지므로 여기서 막는다.
    """
    file = Path(path)
    if file.exists():
        raise CryptoError(f"마스터 키가 이미 있다: {path} (덮어쓰면 기존 데이터를 못 읽는다)")

    material = key or generate_key()
    _require_key(material)
    file.parent.mkdir(parents=True, exist_ok=True)
    # 쓰고 나서 chmod 하면 그 사이에 읽힐 수 있다. 0600으로 열어서 쓴다.
    with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(base64.b64encode(material))
    return material


class Vault:
    """봉투 암호화 창구. 블록은 이 클래스만 쓴다."""

    def __init__(
        self, master_key: bytes, cipher: BaseCipher | None = None, key_id: str = "k1"
    ) -> None:
        _require_key(master_key)
        self._kek = master_key
        self._cipher = cipher or AesGcmCipher()
        self._key_id = key_id

    @property
    def algorithm(self) -> str:
        return self._cipher.algorithm

    @property
    def key_id(self) -> str:
        return self._key_id

    def seal(self, plaintext: bytes, *, aad: bytes = b"") -> SealedData:
        """레코드마다 새 DEK를 만들어 봉인한다.

        DEK를 재사용하면 GCM에서 nonce 재사용 위험이 커지고, 키 하나가 새면
        전체가 열린다. DEK 생성 비용은 32바이트 난수라 무시할 만하다.
        """
        dek = generate_key()
        ciphertext, nonce = self._cipher.encrypt(dek, plaintext, aad=aad)
        wrapped, wrap_nonce = self._cipher.encrypt(self._kek, dek, aad=self._key_id.encode())
        return SealedData(
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_key=wrapped,
            wrap_nonce=wrap_nonce,
            algorithm=self._cipher.algorithm,
            key_id=self._key_id,
        )

    def open(self, sealed: SealedData, *, aad: bytes = b"") -> bytes:
        if sealed.algorithm != self._cipher.algorithm:
            raise CryptoError(
                f"알고리즘이 다르다: 저장={sealed.algorithm}, 현재={self._cipher.algorithm}"
            )
        dek = self._cipher.decrypt(
            self._kek, sealed.wrapped_key, sealed.wrap_nonce, aad=sealed.key_id.encode()
        )
        return self._cipher.decrypt(dek, sealed.ciphertext, sealed.nonce, aad=aad)

    def rewrap(self, sealed: SealedData, new_master_key: bytes, new_key_id: str) -> SealedData:
        """키 교체 — DEK만 새 KEK로 다시 감싼다.

        본문은 건드리지 않으므로 수백 GB의 녹취도 즉시 교체된다. 전체
        재암호화를 요구하는 설계는 폐쇄망에서 사실상 교체 불가를 뜻한다.
        """
        _require_key(new_master_key)
        dek = self._cipher.decrypt(
            self._kek, sealed.wrapped_key, sealed.wrap_nonce, aad=sealed.key_id.encode()
        )
        wrapped, wrap_nonce = self._cipher.encrypt(new_master_key, dek, aad=new_key_id.encode())
        return SealedData(
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            wrapped_key=wrapped,
            wrap_nonce=wrap_nonce,
            algorithm=sealed.algorithm,
            key_id=new_key_id,
        )
