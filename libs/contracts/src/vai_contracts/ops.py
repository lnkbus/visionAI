"""운영 계약 — 운영 콘솔이 보는 플랫폼 상태.

폐쇄망 고객사는 공급사가 원격으로 들여다볼 수 없다. 장애가 나면 고객사 운영자가
**스스로** "어느 블록이 죽었는지, 라이선스가 만료됐는지, 감사 로그가 온전한지"를
확인해야 하고, 그 확인이 안 되면 곧바로 공급사 출동으로 이어진다.
출동 한 번의 원가가 유지보수 수익을 잠식하므로, 이 화면은 비용 구조의 일부다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class BlockState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    """떠 있지만 정상은 아니다(라이선스 만료 유예 등). 지금 죽지는 않았으나
    방치하면 죽는 상태 — 이 구분이 없으면 운영자가 예방 조치를 할 수 없다."""

    DOWN = "down"
    UNKNOWN = "unknown"
    """조회 자체를 못 했다. 블록이 죽은 것과 콘솔이 못 보는 것은 다르다."""


class BlockHealth(BaseModel):
    """블록 하나의 상태."""

    block_id: str
    url: str = ""
    state: BlockState = BlockState.UNKNOWN
    latency_ms: float | None = None
    """응답 지연. 느려지는 블록은 죽기 전에 먼저 느려진다."""

    license_expired: bool = False
    license_in_grace: bool = False
    license_days_remaining: int | None = None
    detail: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlatformStatus(BaseModel):
    """콘솔 첫 화면. 여기만 보고 "지금 정상인가"를 판단할 수 있어야 한다."""

    healthy: bool
    ready_count: int
    total_count: int
    blocks: list[BlockHealth] = Field(default_factory=list)

    license_customer: str = ""
    license_expires: str = ""
    license_days_remaining: int | None = None
    license_signature_verified: bool = False
    """False면 서명 검증 없이 도는 개발 빌드다. 운영에 이 상태로 나가는 것이
    라이선스 사고의 대표 경로라 첫 화면에서 바로 보이게 한다."""

    audit_intact: bool | None = None
    audit_total: int = 0
    audit_broken_at: int | None = None
    encryption_enabled: bool = False
    encryption_algorithm: str = ""

    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
