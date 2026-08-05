"""라이선스 계약 — 폐쇄망 발급 절차의 규격.

망분리 고객사에서 라이선스는 **파일 두 개가 오가는 절차**다:

1. 설치 서버에서 CORE-LIC이 H/W 지문을 담은 발급요청서(``.req``)를 만든다
2. 요청서를 USB로 반출해 공급사 발급기(``licgen``)에 넣는다
3. 서명된 ``.lic``을 반입해 설치한다

이 모듈은 그 두 파일의 형식을 고정한다. 형식이 문서에만 있으면 발급기와
설치기가 조용히 어긋나고, 그 사실은 고객사 현장에서야 드러난다.

**개인키는 이 레포 어디에도 없다.** 발급기가 쓰는 키는 공급사 발급 서버에만
둔다 — 제품 이미지에 들어가는 순간 고객사가 무제한 라이선스를 만들 수 있다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from pydantic import BaseModel, Field


class FingerprintReport(BaseModel):
    """설치 서버의 H/W 지문. 실제 식별자가 아니라 SHA-256 해시만 담는다.

    원본 값(GPU UUID·MAC)을 담으면 발급요청서 자체가 고객사 자산정보가 되어
    반출 승인 절차가 무거워진다. 해시만으로 검증이 성립하므로 원본은 보내지 않는다.
    """

    combined: str
    """전체 해시. 부품이 하나도 안 바뀌었으면 이것만 봐도 된다."""

    components: dict[str, str] = Field(default_factory=dict)
    """부품명 → SHA-256. 부품별로 남겨야 "N개 이상 일치" 완화 정책이 가능하다."""

    hostname: str = ""
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LicenseRequest(BaseModel):
    """발급요청서(``.req``). 설치 서버 → 공급사 발급기."""

    customer_id: str
    site: str = ""
    """설치 사이트 구분. 같은 고객사가 본사·재해복구 센터를 따로 받을 때 쓴다."""

    fingerprint: FingerprintReport
    requested_blocks: list[str] = Field(default_factory=list)
    contact: str = ""
    notes: str = ""


class LicenseInstall(BaseModel):
    """``POST /internal/v1/license`` — 반입한 ``.lic`` 내용을 그대로 싣는다.

    파일 업로드가 아니라 JSON 본문으로 받는 이유: 폐쇄망 운영 콘솔에서
    붙여넣기로도 설치할 수 있어야 한다. USB 마운트 권한이 없는 운영자가 흔하다.
    """

    content: dict[str, object]
    """``{"payload": {...}, "signature": "...", "key_id": "..."}``"""

    activate: bool = True
    """False면 검증만 하고 저장하지 않는다 — 설치 전 사전 점검용."""


class LicenseView(BaseModel):
    """라이선스 현황. 운영 콘솔이 만료 임박을 감시하는 화면의 데이터원."""

    installed: bool
    customer_id: str = ""
    expires: date | None = None
    days_remaining: int | None = None
    in_grace: bool = False
    dev_mode: bool = False
    """서명 검증 없이 도는 개발 모드. 운영 화면에 반드시 노출한다 —
    이 값이 True인 채로 납품되는 것이 라이선스 사고의 대표 경로다."""

    signature_verified: bool = False
    fingerprint_ok: bool = False
    blocks: list[str] = Field(default_factory=list)
    capacity: dict[str, dict[str, int]] = Field(default_factory=dict)
    """블록 → 용량 단위별 상한. 견적서 라인 아이템과 1:1로 대응한다."""
