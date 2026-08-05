"""``block.yaml`` 매니페스트 스키마 — 레고블록 계약의 기계 판독 형태.

문서 ``docs/00-module-catalog.md``의 블록 계약 5원칙 중 4·5번(라이선스 게이팅,
청구 단위)이 여기서 코드로 강제된다. 카탈로그에 블록을 추가한다는 것은
곧 이 매니페스트를 추가한다는 뜻이다.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class BlockTier(StrEnum):
    FOUNDATION = "foundation"
    SPEECH = "speech"
    INTELLIGENCE = "intelligence"
    AUTHORING = "authoring"
    """저작·학습 도구. 고객사가 스스로 운영하기 위한 계층으로, 상담 실시간
    경로와 부하 특성이 완전히 달라 별도 티어로 둔다."""

    EXPERIENCE = "experience"


class CapacityUnit(StrEnum):
    """라이선스 용량 단위. 카탈로그 §4.2."""

    CONCURRENT_CHANNELS = "concurrent_channels"
    AUDIO_HOURS_MONTHLY = "audio_hours_monthly"
    CONCURRENT_STREAMS = "concurrent_streams"
    AVATAR_SESSIONS = "avatar_sessions"
    SEATS = "seats"
    INDEX_CHUNKS = "index_chunks"
    NONE = "none"
    """기반 블록 — 패키지에 포함되며 별도 과금하지 않는다."""


class Billing(BaseModel):
    """청구 단위 정의 — 견적서 라인 아이템의 원천."""

    capacity_unit: CapacityUnit
    dev_effort_mm: float = 0.0
    """구축 개발비 산정용 참고 MM."""

    included_in_package: bool = False
    """True면 패키지 가격에 포함(별도 라이선스 미과금)."""


class Contracts(BaseModel):
    """블록이 참여하는 이벤트 토픽. 조립 검증에 쓴다."""

    consumes: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    openapi: str | None = None


class BlockManifest(BaseModel):
    """``blocks/<dir>/block.yaml``."""

    id: str
    """카탈로그 ID. 예: ``STT-CORE``."""

    name: str
    tier: BlockTier
    version: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    """의존 블록 ID. 순환 의존은 blockctl이 거부한다."""

    adapters: list[str] = Field(default_factory=list)
    """교체 가능한 엔진 어댑터 이름(핫스왑 대상)."""

    contracts: Contracts = Field(default_factory=Contracts)
    billing: Billing
    license_key: str | None = None
    """``.lic`` 파일에서 이 블록을 게이팅하는 키. 미지정 시 ``id``를 사용."""

    @property
    def gate_key(self) -> str:
        return self.license_key or self.id
