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
    """**기동 의존** 블록 ID. 이것이 없으면 블록이 제 역할을 못 한다.
    순환 의존은 blockctl이 거부한다."""

    recommends: list[str] = Field(default_factory=list)
    """**기능 의존** 블록 ID. 없어도 기동하지만 기능 하나가 죽는다.

    UI-AGENT는 TA-ASSIST 없이도 뜬다 — 자막과 컴플라이언스 경고는 나온다.
    다만 지식 팝업 패널이 영원히 비어 있다. 기동 의존으로 적으면 compose가
    기동 순서를 잘못 묶고, 안 적으면 번들에서 조용히 빠져 "패널이 죽은 채로"
    납품된다. 그래서 별도 항목으로 둔다 — 에어갭 번들은 이것까지 담는다."""

    adapters: list[str] = Field(default_factory=list)
    """교체 가능한 엔진 어댑터 이름(핫스왑 대상)."""

    infra: list[str] = Field(default_factory=list)
    """이 블록이 필요로 하는 외부 인프라 이미지(``redis``, ``qdrant`` 등).

    에어갭 번들에 무엇을 함께 반입할지 계산하는 근거다. 손으로 목록을 만들면
    반드시 하나가 빠지고, 그 사실은 반입 승인이 끝난 고객사 현장에서야 드러난다."""

    contracts: Contracts = Field(default_factory=Contracts)
    billing: Billing
    license_key: str | None = None
    """``.lic`` 파일에서 이 블록을 게이팅하는 키. 미지정 시 ``id``를 사용."""

    @property
    def gate_key(self) -> str:
        return self.license_key or self.id
