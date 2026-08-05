"""라이선스 게이트 클라이언트 (CORE-LIC 스텁 — Wave 1).

블록 활성화와 용량 제한을 조회하는 **인터페이스**를 지금 고정해 둔다. 각 블록은
처음부터 이 게이트를 통해 기동하므로, Wave 6에서 실제 DRM(H/W Fingerprint 수집 +
RSA-4096 서명 검증)을 붙일 때 블록 코드는 손대지 않는다.

현 단계에서 구현된 것: ``.lic`` JSON 파싱, 블록/용량 게이팅, 만료 확인.
Wave 6에서 추가될 것: 서명 검증, 하드웨어 지문 대조, 메모리 내 복호화.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

log = logging.getLogger(__name__)


class LicenseError(RuntimeError):
    """라이선스 위반 — 기동 거부 사유."""


@dataclass(frozen=True)
class BlockGrant:
    enabled: bool
    capacity: dict[str, int]

    def limit(self, unit: str, default: int = 0) -> int:
        return self.capacity.get(unit, default)


class LicenseGate:
    def __init__(self, grants: dict[str, BlockGrant], expires: date | None, dev_mode: bool) -> None:
        self._grants = grants
        self._expires = expires
        self._dev_mode = dev_mode

    @classmethod
    def load(cls, path: str | None) -> LicenseGate:
        if not path:
            log.warning("라이선스 파일 미지정 — 개발 모드로 모든 블록을 허용한다")
            return cls({}, None, dev_mode=True)

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        expires_raw = raw.get("expires")
        expires = date.fromisoformat(expires_raw) if expires_raw else None
        grants = {
            block_id: BlockGrant(
                enabled=bool(spec.get("enabled", True)),
                capacity={k: int(v) for k, v in spec.items() if isinstance(v, int)},
            )
            for block_id, spec in raw.get("blocks", {}).items()
        }
        return cls(grants, expires, dev_mode=False)

    @property
    def dev_mode(self) -> bool:
        return self._dev_mode

    def is_expired(self) -> bool:
        return self._expires is not None and datetime.now(UTC).date() > self._expires

    def require(self, block_id: str) -> BlockGrant:
        """블록 기동 시 호출. 미허가면 :class:`LicenseError`."""
        if self._dev_mode:
            return BlockGrant(enabled=True, capacity={})
        if self.is_expired():
            raise LicenseError(f"라이선스 만료: {self._expires}")
        grant = self._grants.get(block_id)
        if grant is None or not grant.enabled:
            raise LicenseError(f"블록 '{block_id}'은 라이선스에 포함되지 않았다")
        return grant
