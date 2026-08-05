"""오프라인 라이선스 — 사양서 §5의 Hardware-Bound DRM.

폐쇄망에서 라이선스를 검증하는 유일한 방법은 **서명된 파일**이다. 서버에
물어볼 수 없으니 파일 자체가 진위를 증명해야 한다. RSA-4096 개인키로 서명하고,
제품에 내장된 공개키로 검증한다. 공개키만 배포되므로 고객사가 라이선스를
위조할 수 없다.

**call-home은 하지 않는다.** 망분리 환경에서 외부 통신을 시도하는 것 자체가
보안 심사 감점 요인이고, 애초에 나가지도 못한다.

모든 블록이 같은 ``.lic`` 파일을 읽어 각자 검증한다. 라이선스 서버를 두면
그 서버가 죽었을 때 전 블록이 기동하지 못한다 — 폐쇄망에서 가장 피해야 할
단일 장애점이다.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from vai_common.fingerprint import DEFAULT_REQUIRED_MATCHES, collect, from_components

log = logging.getLogger(__name__)

GRACE_DAYS = 30
"""만료 후 유예 기간. 갱신이 늦어졌다고 상담을 즉시 끊으면 고객사 업무가 멈춘다.
경고를 띄우고 신규 세션만 제한한다(docs/03 §4)."""

SIGNATURE_ALGORITHM = "RSA-4096/PSS-SHA256"

EMBEDDED_PUBLIC_KEY = Path(__file__).parent / "keys" / "license_pub.pem"
"""릴리스 빌드가 심는 공개키. 레포에는 없다 — 개발 체크아웃에서 서명 검증이
자동으로 켜지면 개발자가 매번 라이선스를 발급해야 하기 때문이다.

``deploy/`` 릴리스 절차가 발급용 키쌍의 공개키만 이 경로에 복사한다."""


class LicenseError(RuntimeError):
    """라이선스 위반 — 기동 거부 사유."""


class LicenseSignatureError(LicenseError):
    """서명 검증 실패. 위조이거나 다른 키로 발급된 파일이다."""


def resolve_public_key(path: str | None) -> bytes | None:
    """검증에 쓸 공개키를 고른다: 설정 경로 → 내장 키 → 없음.

    없으면 ``None``을 돌려주고 호출부가 경고를 남긴다. 여기서 예외를 던지면
    개발 체크아웃에서 아무 블록도 기동하지 못한다.
    """
    if path:
        configured = Path(path)
        if not configured.is_file():
            # 경로를 지정했는데 없다는 것은 배포 실수다. 조용히 검증을 끄면
            # 서명 없는 라이선스가 운영에 들어간다 — 반드시 기동을 막는다.
            raise LicenseError(f"공개키 파일을 찾을 수 없다: {path}")
        return configured.read_bytes()
    if EMBEDDED_PUBLIC_KEY.is_file():
        return EMBEDDED_PUBLIC_KEY.read_bytes()
    return None


@dataclass(frozen=True)
class BlockGrant:
    enabled: bool
    capacity: dict[str, int]

    def limit(self, unit: str, default: int = 0) -> int:
        return self.capacity.get(unit, default)


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """서명 대상 바이트를 정규화한다.

    딕셔너리 순서나 공백이 달라지면 서명이 깨진다. 발급기와 검증기가 **반드시**
    같은 규칙을 써야 하므로 한 함수로 묶어 둔다.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_payload(
    payload: dict[str, Any], private_key_pem: bytes, password: bytes | None = None
) -> str:
    """라이선스 페이로드에 서명한다(발급기 전용).

    운영에서는 개인키가 사내 발급 서버에만 있어야 한다. 제품 이미지에 들어가면
    고객사가 무제한 라이선스를 스스로 만들 수 있다.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = serialization.load_pem_private_key(private_key_pem, password=password)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise LicenseError("RSA 개인키가 아니다")
    if key.key_size < 4096:
        raise LicenseError(f"RSA-4096 이상이어야 한다 (현재 {key.key_size})")

    signature = key.sign(
        canonical_payload(payload),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def verify_signature(payload: dict[str, Any], signature_b64: str, public_key_pem: bytes) -> None:
    """서명을 검증한다. 실패하면 :class:`LicenseSignatureError`."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(key, rsa.RSAPublicKey):
        raise LicenseSignatureError("RSA 공개키가 아니다")

    try:
        key.verify(
            base64.b64decode(signature_b64),
            canonical_payload(payload),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise LicenseSignatureError("라이선스 서명이 유효하지 않다") from exc


@dataclass
class LicenseStatus:
    """라이선스 현황. 운영 콘솔이 만료 임박을 감시하는 데 쓴다."""

    customer_id: str
    expires: date | None
    days_remaining: int | None
    in_grace: bool
    dev_mode: bool
    blocks: list[str]
    fingerprint_ok: bool
    signature_verified: bool = False
    """서명을 실제로 검증했는지. False면 공개키 없이 읽은 개발 경로다."""

    capacity: dict[str, dict[str, int]] = field(default_factory=dict)
    """블록 → 용량 단위별 상한."""


class LicenseGate:
    """블록 기동 시 라이선스를 확인하는 관문."""

    def __init__(
        self,
        grants: dict[str, BlockGrant],
        expires: date | None,
        dev_mode: bool,
        *,
        customer_id: str = "",
        fingerprint_ok: bool = True,
        signature_verified: bool = False,
    ) -> None:
        self._grants = grants
        self._expires = expires
        self._dev_mode = dev_mode
        self._customer_id = customer_id
        self._fingerprint_ok = fingerprint_ok
        self._signature_verified = signature_verified

    @classmethod
    def load(
        cls,
        path: str | None,
        *,
        public_key_pem: bytes | None = None,
        verify_fingerprint: bool = True,
        required_matches: int = DEFAULT_REQUIRED_MATCHES,
    ) -> LicenseGate:
        """``.lic`` 파일을 읽고 검증한다.

        ``public_key_pem``이 없으면 서명 검증을 건너뛴다 — 개발·테스트 경로다.
        운영 이미지는 반드시 공개키를 내장해 전달한다.
        """
        if not path:
            log.warning("라이선스 파일 미지정 — 개발 모드로 모든 블록을 허용한다")
            return cls({}, None, dev_mode=True)

        file = Path(path)
        if not file.is_file():
            if public_key_pem is not None:
                # 릴리스 빌드(공개키 내장)에서 파일이 없다 = 라이선스가 없다.
                # 여기서 열어 주면 ".lic을 지우면 무제한"이 되어 DRM이 무의미해진다.
                raise LicenseError(f"라이선스 파일이 없다: {path}")
            log.warning("라이선스 파일이 아직 없다 — 개발 모드로 기동한다: %s", path)
            return cls({}, None, dev_mode=True)

        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 잘린 JSON이 남으면 전 블록이 기동하지 못한다. 원인이 파일이라는
            # 사실이 로그 한 줄로 보여야 현장에서 복구할 수 있다.
            raise LicenseError(f"라이선스 파일을 읽을 수 없다: {path} ({exc})") from exc

        return cls.from_document(
            document,
            public_key_pem=public_key_pem,
            verify_fingerprint=verify_fingerprint,
            required_matches=required_matches,
        )

    @classmethod
    def from_document(
        cls,
        raw: dict[str, Any],
        *,
        public_key_pem: bytes | None = None,
        verify_fingerprint: bool = True,
        required_matches: int = DEFAULT_REQUIRED_MATCHES,
    ) -> LicenseGate:
        """이미 읽어 들인 라이선스 문서를 검증한다.

        설치기(CORE-LIC)가 파일로 쓰기 **전에** 같은 경로로 검증하려고 분리했다.
        설치 시 검증과 기동 시 검증이 다른 코드면, 설치는 통과했는데 재기동에서
        블록이 죽는 사고가 난다.
        """
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise LicenseError("라이선스 파일에 payload가 없다")

        if public_key_pem:
            signature = raw.get("signature")
            if not signature:
                raise LicenseSignatureError("서명이 없는 라이선스 파일이다")
            verify_signature(payload, str(signature), public_key_pem)
        else:
            log.warning("공개키 미지정 — 서명 검증 없이 라이선스를 읽는다(개발 경로)")

        fingerprint_ok = True
        recorded = payload.get("fingerprint", {}).get("components")
        if verify_fingerprint and recorded:
            fingerprint_ok = collect().matches(
                from_components(dict(recorded)), required=required_matches
            )
            if not fingerprint_ok:
                # 다른 서버로 복사된 라이선스. 지문 불일치는 만료와 달리
                # 유예하지 않는다 — 복제 방지가 DRM의 존재 이유다.
                raise LicenseError("라이선스가 이 서버의 H/W 지문과 일치하지 않는다")

        expires_raw = payload.get("expires")
        grants = {
            block_id: BlockGrant(
                enabled=bool(spec.get("enabled", True)),
                # bool은 파이썬에서 int의 하위형이다. 걸러 내지 않으면
                # ``enabled: true``가 용량 1로 둔갑해 동시 채널 1개짜리
                # 라이선스가 되어 버린다.
                capacity={
                    k: int(v)
                    for k, v in spec.items()
                    if isinstance(v, int) and not isinstance(v, bool)
                },
            )
            for block_id, spec in dict(payload.get("blocks", {})).items()
        }
        return cls(
            grants,
            date.fromisoformat(expires_raw) if expires_raw else None,
            dev_mode=False,
            customer_id=str(payload.get("customer_id", "")),
            fingerprint_ok=fingerprint_ok,
            signature_verified=bool(public_key_pem),
        )

    @property
    def dev_mode(self) -> bool:
        return self._dev_mode

    def is_expired(self) -> bool:
        return self._expires is not None and datetime.now(UTC).date() > self._expires

    def in_grace_period(self) -> bool:
        """만료됐지만 아직 유예 중인지."""
        if self._expires is None or not self.is_expired():
            return False
        return datetime.now(UTC).date() <= self._expires + timedelta(days=GRACE_DAYS)

    def days_remaining(self) -> int | None:
        if self._expires is None:
            return None
        return (self._expires - datetime.now(UTC).date()).days

    def require(self, block_id: str) -> BlockGrant:
        """블록 기동 시 호출. 미허가면 :class:`LicenseError`."""
        if self._dev_mode:
            return BlockGrant(enabled=True, capacity={})

        if self.is_expired() and not self.in_grace_period():
            raise LicenseError(f"라이선스 만료: {self._expires} (유예 {GRACE_DAYS}일 경과)")
        if self.is_expired():
            log.warning(
                "라이선스 만료 — 유예 기간 중",
                extra={"expires": str(self._expires), "block": block_id},
            )

        grant = self._grants.get(block_id)
        if grant is None or not grant.enabled:
            raise LicenseError(f"블록 '{block_id}'은 라이선스에 포함되지 않았다")
        return grant

    def status(self) -> LicenseStatus:
        return LicenseStatus(
            customer_id=self._customer_id,
            expires=self._expires,
            days_remaining=self.days_remaining(),
            in_grace=self.in_grace_period(),
            dev_mode=self._dev_mode,
            blocks=sorted(block_id for block_id, grant in self._grants.items() if grant.enabled),
            fingerprint_ok=self._fingerprint_ok,
            signature_verified=self._signature_verified,
            capacity={
                block_id: dict(grant.capacity)
                for block_id, grant in self._grants.items()
                if grant.enabled and grant.capacity
            },
        )
