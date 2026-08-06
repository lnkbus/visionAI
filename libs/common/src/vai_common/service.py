"""블록 서비스 부트스트랩.

모든 블록이 같은 방식으로 기동하도록 공통 절차를 한곳에 모은다:
로깅 구성 → 라이선스 게이트 확인 → 헬스 엔드포인트 노출.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from vai_common.license import LicenseError, LicenseGate, resolve_public_key
from vai_common.logging import configure_logging
from vai_common.settings import CommonSettings, get_settings

log = logging.getLogger(__name__)

Lifespan = Callable[[FastAPI], Any]


def create_block_app(
    *,
    block_id: str,
    title: str,
    settings: CommonSettings | None = None,
    lifespan: Lifespan | None = None,
    license_optional: bool = False,
) -> FastAPI:
    """블록용 FastAPI 앱을 만든다.

    ``lifespan``으로 블록 고유의 기동/종료 처리를 넘기면 공통 절차 뒤에 실행된다.

    ``license_optional``은 **CORE-LIC 전용**이다. 라이선스를 설치하는 블록이
    라이선스 없이는 못 뜬다면 신규 구축에서 설치 자체가 불가능하고, 만료·지문
    불일치로 멈춘 현장에서는 교체 라이선스를 넣을 창구가 사라진다.
    다른 블록에 붙이면 그 블록은 게이팅에서 빠진다 — 붙이지 않는다.
    """
    cfg = settings or get_settings()
    configure_logging(block_id, cfg.log_level, as_json=cfg.log_json)

    try:
        gate = LicenseGate.load(
            cfg.license_path,
            public_key_pem=resolve_public_key(cfg.license_public_key_path),
            verify_fingerprint=cfg.license_verify_fingerprint,
        )
        gate.require(block_id)  # 미허가면 여기서 기동이 중단된다
    except LicenseError:
        if not license_optional:
            raise
        # 설치 창구는 살려 둔다. 이 블록이 할 수 있는 일은 라이선스 설치뿐이므로
        # 열어 두어도 다른 기능이 게이팅을 우회하지는 않는다.
        log.warning("유효한 라이선스 없이 기동 — 설치 창구", extra={"block": block_id})
        gate = LicenseGate({}, None, dev_mode=True)

    if gate.dev_mode:
        log.info("개발 모드 기동", extra={"block": block_id})

    @asynccontextmanager
    async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
        log.info("블록 기동", extra={"block": block_id, "env": cfg.environment})
        async with AsyncExitStack() as stack:
            if lifespan is not None:
                await stack.enter_async_context(lifespan(application))
            yield
        log.info("블록 종료", extra={"block": block_id})

    app = FastAPI(title=title, version="0.1.0", lifespan=_lifespan)
    app.state.block_id = block_id
    app.state.settings = cfg
    app.state.license = gate

    @app.get("/", include_in_schema=False)
    async def root() -> HTMLResponse:
        """포트를 브라우저에 그냥 쳐 본 사람에게 답한다.

        이전에는 `{"detail":"Not Found"}` 였다. 틀린 말은 아니지만 **다음에
        무엇을 할지가 없다.** 화면이 안 뜬다고 할 때 열어 본 곳이 대개
        여기고, 거기서 막힌다. 블록이 무엇이고 어디를 보면 되는지 적는다.
        """
        return HTMLResponse(_landing_page(block_id, title))

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """프로세스 생존 확인 (K8s liveness)."""
        return {"status": "ok", "block": block_id}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, object]:
        """트래픽 수용 준비 확인 (K8s readiness).

        만료 잔여일을 함께 실어 CORE-ADM이 블록별로 긁어 갈 수 있게 한다.
        라이선스 만료는 예고 없이 오면 사고지만, 미리 보이면 그냥 갱신 업무다.
        """
        return {
            "status": "degraded" if gate.is_expired() else "ready",
            "block": block_id,
            "license_expired": gate.is_expired(),
            "license_in_grace": gate.in_grace_period(),
            "license_days_remaining": gate.days_remaining(),
        }

    return app


def _landing_page(block_id: str, title: str) -> str:
    """블록 자기소개. 바깥 자원을 안 쓴다 — 폐쇄망에서 CDN 은 그냥 빈 화면이다."""
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<title>{block_id}</title>
<style>
 body{{font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;display:grid;place-items:center;min-height:100vh;color:#1b1f24}}
 main{{max-width:34rem;padding:2rem}}
 h1{{font-size:1.25rem;margin:0}} p{{color:#57606a}}
 a{{color:#0969da}} ul{{padding-left:1.1rem}}
</style>
<main>
 <h1>{block_id}</h1>
 <p>{title}</p>
 <p>이 블록에는 화면이 없다. API 블록이다.</p>
 <ul>
  <li><a href="/docs">/docs</a> — 이 블록이 받는 요청</li>
  <li><a href="/healthz">/healthz</a> — 살아 있는가</li>
  <li><a href="/readyz">/readyz</a> — 받을 준비가 됐는가(라이선스 잔여일 포함)</li>
 </ul>
 <p>화면은 다른 포트에 있다. <code>docs/13</code> 에 주소가 정리돼 있다.</p>
</main></html>"""


def set_landing(app: FastAPI, path: str) -> None:
    """이 블록의 첫 화면을 정한다. `/` 로 들어오면 그리로 보낸다.

    화면을 가진 블록인데 `/` 가 404 면, 포트만 알고 열어 본 사람은 화면이
    있다는 사실 자체를 모른다. 실제로 `localhost:8094` 를 열고
    `{"detail":"Not Found"}` 를 받은 일이 있었다 — 회의록 화면은
    `/minutes` 에 멀쩡히 떠 있었다.

    화면이 설정으로 꺼질 수 있는 블록(저작 콘솔·데모)이 있어서 `create_block_app`
    인자가 아니라 별도 함수다. 켤 때만 부른다.
    """
    # 기본 자기소개를 걷어낸다. FastAPI 는 먼저 걸린 경로가 이기므로,
    # 그냥 더 붙이면 새 것이 영원히 안 불린다.
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", "") != "/"]

    @app.get("/", include_in_schema=False)
    async def _to_screen() -> RedirectResponse:
        return RedirectResponse(path)


def serve(app: FastAPI, *, default_port: int) -> None:
    """블록 서버를 기동한다.

    포트를 하드코딩하면 한 호스트에서 여러 블록을 띄우는 구성(compose 데모,
    스택 스모크 테스트, 단일 노드 PoC)에서 충돌한다. ``VAI_PORT``로 덮어쓴다.
    """
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("VAI_HOST", "0.0.0.0"),
        port=int(os.environ.get("VAI_PORT", default_port)),
        log_config=None,  # configure_logging이 세운 핸들러를 uvicorn이 덮어쓰지 않게 한다
    )
