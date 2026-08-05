"""발급 로직 — 요청서를 서명된 ``.lic``으로 바꾼다.

CLI와 분리해 두면 발급 서버가 나중에 웹 폼이나 ERP 연동으로 바뀌어도
서명 규칙은 그대로 재사용된다. 서명 규칙이 갈라지면 이미 나간 라이선스가
검증에 실패한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from vai_common.license import SIGNATURE_ALGORITHM, sign_payload, verify_signature
from vai_contracts.licensing import LicenseRequest

LICENSE_FORMAT = 1
"""라이선스 문서 버전. 형식을 바꿀 때 올려서 구버전 제품이 거부할 수 있게 한다."""

_GRANT_PATTERN = re.compile(r"^(?P<block>[A-Za-z0-9._-]+)(?::(?P<unit>\w+)=(?P<limit>\d+))?$")


class IssueError(ValueError):
    """발급 입력 오류. 잘못된 라이선스를 내보내느니 여기서 멈춘다."""


@dataclass(frozen=True)
class IssuedLicense:
    document: dict[str, Any]

    @property
    def payload(self) -> dict[str, Any]:
        return dict(self.document["payload"])

    def dumps(self) -> str:
        import json

        return json.dumps(self.document, ensure_ascii=False, indent=2, sort_keys=True)


def generate_key_pair(bits: int = 4096) -> tuple[bytes, bytes]:
    """발급용 RSA 키쌍(개인키 PEM, 공개키 PEM)을 만든다.

    공개키만 제품에 내장하고 개인키는 발급 서버에 남긴다. 키를 교체하려면
    새 키로 서명한 라이선스와 새 공개키를 담은 제품 버전을 함께 배포해야 하므로,
    한번 정한 키는 오래 간다 — 보관을 그만큼 신중히 한다.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if bits < 4096:
        raise IssueError(f"RSA-4096 이상이어야 한다 (요청: {bits})")

    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
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


def parse_grant(spec: str) -> tuple[str, dict[str, int]]:
    """``STT-CORE`` 또는 ``STT-CORE:concurrent_channels=50``을 해석한다."""
    match = _GRANT_PATTERN.match(spec.strip())
    if match is None:
        raise IssueError(
            f"블록 지정 형식이 잘못됐다: '{spec}' (예: STT-CORE:concurrent_channels=50)"
        )
    block = match.group("block").upper()
    unit, limit = match.group("unit"), match.group("limit")
    return block, ({unit: int(limit)} if unit else {})


def catalog_block_ids(root: Path) -> set[str]:
    """``blocks/*/block.yaml``의 ID 집합. 오타 검증용."""
    import yaml

    ids: set[str] = set()
    for manifest in sorted((root / "blocks").glob("*/block.yaml")):
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        block_id = raw.get("license_key") or raw.get("id")
        if block_id:
            ids.add(str(block_id))
    return ids


def build_payload(
    request: LicenseRequest,
    *,
    expires: date | None,
    grants: dict[str, dict[str, int]],
    issued_on: date,
    key_id: str,
) -> dict[str, Any]:
    """서명 대상 페이로드를 만든다.

    ``fingerprint``는 요청서에서 온 값을 그대로 옮긴다. 발급기가 지문을
    가공하면 설치 서버에서 검증이 깨진다.
    """
    if not grants:
        raise IssueError("허가할 블록이 하나도 없다 — 아무것도 기동하지 못하는 라이선스다")

    return {
        "format": LICENSE_FORMAT,
        "customer_id": request.customer_id,
        "site": request.site,
        "issued_on": issued_on.isoformat(),
        "expires": expires.isoformat() if expires else None,
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "fingerprint": {
            "combined": request.fingerprint.combined,
            "components": dict(request.fingerprint.components),
        },
        "blocks": {
            block: {"enabled": True, **capacity} for block, capacity in sorted(grants.items())
        },
    }


def issue(
    request: LicenseRequest,
    *,
    private_key_pem: bytes,
    expires: date | None,
    grants: dict[str, dict[str, int]],
    issued_on: date,
    key_id: str = "default",
    password: bytes | None = None,
) -> IssuedLicense:
    """요청서를 서명된 라이선스 문서로 만든다."""
    if expires is not None and expires <= issued_on:
        raise IssueError(f"만료일이 발급일보다 앞선다: {expires} <= {issued_on}")
    if not request.fingerprint.components:
        # 지문 없는 요청서는 어느 서버에서든 도는 라이선스가 된다.
        raise IssueError("요청서에 H/W 지문이 없다 — 이 요청서로는 발급할 수 없다")

    payload = build_payload(
        request, expires=expires, grants=grants, issued_on=issued_on, key_id=key_id
    )
    signature = sign_payload(payload, private_key_pem, password=password)
    # 발급 직후 제품과 같은 검증기로 한 번 되짚는다. 서명이 깨진 라이선스는
    # 고객사 현장에서야 드러나고, 폐쇄망에서는 재반입에 며칠이 걸린다.
    verify_signature(payload, signature, _public_pem(private_key_pem))
    return IssuedLicense({"payload": payload, "signature": signature, "key_id": key_id})


def _public_pem(private_key_pem: bytes, password: bytes | None = None) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_key_pem, password=password)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
