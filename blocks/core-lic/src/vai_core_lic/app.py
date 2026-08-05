"""CORE-LIC — 오프라인 DRM 라이선스 블록.

폐쇄망 발급 절차의 서버 쪽 창구다:

1. ``GET  /internal/v1/license/fingerprint`` — 이 서버의 H/W 지문
2. ``POST /internal/v1/license/request``     — 반출용 발급요청서(``.req``)
3. ``POST /internal/v1/license``             — 반입한 ``.lic`` 검증·설치
4. ``GET  /internal/v1/license``             — 현황(만료 잔여일·블록·용량)

**이 블록은 다른 블록을 게이팅하지 않는다.** 각 블록이 기동 시 같은 ``.lic``을
직접 검증한다(``vai_common.service.create_block_app``). 라이선스 서버를 두면
그 서버가 죽는 순간 폐쇄망 전체가 기동하지 못한다 — 가장 피해야 할 단일 장애점이다.
CORE-LIC이 죽어도 이미 설치된 라이선스로 상담은 계속된다.

엔드포인트를 ``/internal``에 둔 이유: 라이선스 설치는 운영자 행위이므로
CORE-GW의 인증을 통과한 운영 콘솔(CORE-ADM)만 접근해야 한다. 외부에 열지 않는다.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from vai_common.license import LicenseError, resolve_public_key
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.licensing import (
    FingerprintReport,
    LicenseInstall,
    LicenseRequest,
    LicenseView,
)
from vai_core_lic import BLOCK_ID
from vai_core_lic.installer import LicenseInstaller, build_request, current_fingerprint

log = logging.getLogger(__name__)


class RequestForm(BaseModel):
    """발급요청서 작성 입력. 운영자가 콘솔 화면에서 채운다."""

    customer_id: str
    site: str = ""
    contact: str = ""
    notes: str = ""
    requested_blocks: list[str] = Field(default_factory=list)
    include_gpu: bool = True
    """GPU를 교체·증설할 예정이면 False. 지문이 약해지는 대신 유지보수 중
    라이선스가 깨지지 않는다 — 고객사와 합의할 사항이다."""


def create_app(installer: LicenseInstaller | None = None) -> FastAPI:
    common = get_settings()
    store = installer or LicenseInstaller(
        common.license_path,
        public_key_pem=resolve_public_key(common.license_public_key_path),
        verify_fingerprint=common.license_verify_fingerprint,
    )

    # 라이선스가 없거나 만료·지문 불일치인 상태에서도 떠야 한다. 이 블록이
    # 못 뜨면 신규 구축에서 최초 설치가 불가능하고, 멈춘 현장에서는 교체
    # 라이선스를 넣을 창구가 없어진다.
    app = create_block_app(
        block_id=BLOCK_ID,
        title="VisionAI CORE-LIC",
        settings=common,
        license_optional=True,
    )
    app.state.installer = store

    if not store.signing_enforced:
        # 공개키 없이 기동하면 서명 없는 라이선스도 통과한다. 개발에서는
        # 정상이지만 운영에 이 상태로 나가면 DRM이 없는 것과 같다.
        log.warning("공개키가 없어 서명 검증이 비활성화됐다 — 릴리스 빌드가 아니다")

    @app.get("/internal/v1/license/fingerprint", response_model=FingerprintReport, tags=["license"])
    async def fingerprint(include_gpu: bool = True) -> FingerprintReport:
        """이 서버의 H/W 지문(해시만). 발급 문의 시 첨부한다."""
        return current_fingerprint(include_gpu=include_gpu)

    @app.post("/internal/v1/license/request", response_model=LicenseRequest, tags=["license"])
    async def create_request(form: RequestForm) -> LicenseRequest:
        """반출용 발급요청서를 만든다. 응답 JSON을 그대로 ``.req``로 저장한다."""
        try:
            return build_request(
                form.customer_id,
                site=form.site,
                contact=form.contact,
                notes=form.notes,
                requested_blocks=form.requested_blocks,
                include_gpu=form.include_gpu,
            )
        except LicenseError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/internal/v1/license", response_model=LicenseView, tags=["license"])
    async def install(payload: LicenseInstall, request: Request) -> LicenseView:
        """반입한 ``.lic``을 검증하고 설치한다.

        ``activate=false``면 검증만 한다. 점검 없이 설치했다가 지문이 어긋나면
        재기동 시점에야 알게 되고, 그때는 이미 서비스가 멈춘 뒤다.
        """
        store_: LicenseInstaller = request.app.state.installer
        try:
            if not payload.activate:
                return store_.verify(payload.content)
            return store_.install(payload.content)
        except LicenseError as exc:
            # 400 — 파일이 잘못됐다는 뜻이다. 500이면 운영자가 서버 장애로 오해한다.
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"라이선스 파일 쓰기 실패: {exc}"
            ) from exc

    @app.get("/internal/v1/license", response_model=LicenseView, tags=["license"])
    async def status_view(request: Request) -> LicenseView:
        """설치된 라이선스 현황. CORE-ADM 대시보드의 데이터원."""
        store_: LicenseInstaller = request.app.state.installer
        return store_.current()

    return app
