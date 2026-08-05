"""블록 상태 수집.

**모든 블록을 동시에, 짧은 타임아웃으로 찌른다.** 순차로 돌면 죽은 블록 하나마다
타임아웃이 쌓여 화면이 수십 초 멈춘다. 장애 상황에서 가장 느려지는 화면이
장애 확인용 화면이라면 그건 없는 것만 못하다.

**응답 없음과 '죽음'을 구분한다.** 콘솔이 못 보는 것과 블록이 죽은 것은 다르다.
방화벽·DNS 문제를 블록 장애로 오인하면 조사 방향이 통째로 어긋난다.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from vai_contracts.ops import BlockHealth, BlockState

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS: dict[str, str] = {
    "CORE-BUS": "http://core-bus:8081",
    "CORE-GW": "http://core-gw:8080",
    "AUD-VAD": "http://aud-vad:8082",
    "STT-CORE": "http://stt-core:8083",
    "FLT-MICRO": "http://flt-micro:8084",
    "LLM-GW": "http://llm-gw:8085",
    "RAG-KB": "http://rag-kb:8086",
    "RAG-SRCH": "http://rag-srch:8087",
    "TA-ASSIST": "http://ta-assist:8088",
    "LLM-SUM": "http://llm-sum:8089",
    "SCN-STUDIO": "http://scn-studio:8090",
    "UI-AGENT": "http://ui-agent:8091",
    "AUD-RTP": "http://aud-rtp:8092",
    "SPK-DIA": "http://spk-dia:8093",
    "UI-MEET": "http://ui-meet:8094",
    "CORE-LIC": "http://core-lic:8095",
    "CORE-SEC": "http://core-sec:8096",
}
"""compose 기본 배치. 실제 구성은 ``VAI_ADM_ENDPOINTS``(JSON)로 덮어쓴다 —
고객사마다 어떤 블록을 샀는지가 다르므로 목록을 코드에 못 박을 수 없다."""

PROBE_TIMEOUT = 2.0
"""블록 하나당 상한. 길게 잡으면 장애 시 화면이 멈추고, 짧으면 부하 중인
정상 블록을 죽었다고 표시한다. 2초는 헬스 응답으로는 충분히 긴 시간이다."""


async def probe(client: httpx.AsyncClient, block_id: str, url: str) -> BlockHealth:
    """블록 하나의 ``/readyz``를 찌른다. 예외를 밖으로 내보내지 않는다."""
    started = time.perf_counter()
    try:
        # httpx의 timeout은 connect/read 등 단계별 상한이라, 바이트를 조금씩
        # 흘려 보내는 블록에는 전체 상한이 걸리지 않는다. 프로브 하나가 화면
        # 전체를 잡아먹지 않도록 바깥에서 한 번 더 조인다.
        response = await asyncio.wait_for(
            client.get(f"{url.rstrip('/')}/readyz", timeout=PROBE_TIMEOUT),
            timeout=PROBE_TIMEOUT,
        )
    except (httpx.TimeoutException, TimeoutError):
        return BlockHealth(
            block_id=block_id, url=url, state=BlockState.DOWN, detail="응답 시간 초과"
        )
    except httpx.HTTPError as exc:
        # 연결 자체가 안 된 것. 블록이 죽었을 수도, 콘솔이 못 보는 것일 수도 있다.
        return BlockHealth(
            block_id=block_id, url=url, state=BlockState.UNKNOWN, detail=f"연결 실패: {exc}"
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if response.status_code != httpx.codes.OK:
        return BlockHealth(
            block_id=block_id,
            url=url,
            state=BlockState.DOWN,
            latency_ms=elapsed_ms,
            detail=f"HTTP {response.status_code}",
        )

    body = response.json()
    expired = bool(body.get("license_expired"))
    in_grace = bool(body.get("license_in_grace"))
    return BlockHealth(
        block_id=block_id,
        url=url,
        state=BlockState.DEGRADED if expired else BlockState.READY,
        latency_ms=elapsed_ms,
        license_expired=expired,
        license_in_grace=in_grace,
        license_days_remaining=body.get("license_days_remaining"),
        detail=str(body.get("status", "")),
    )


async def probe_all(client: httpx.AsyncClient, endpoints: dict[str, str]) -> list[BlockHealth]:
    """전 블록을 동시에 찌른다. 총 소요는 가장 느린 하나에 수렴한다."""
    results = await asyncio.gather(
        *(probe(client, block_id, url) for block_id, url in sorted(endpoints.items()))
    )
    return list(results)
