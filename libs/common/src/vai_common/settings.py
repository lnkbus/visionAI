"""블록 공통 설정.

환경 분기 금지 원칙(docs/06 §2-4)에 따라 ``if is_onprem:`` 같은 코드 분기 대신
어댑터 이름과 엔드포인트를 설정으로 주입받는다. 온프렘/SaaS 차이는 전부
values 오버레이가 채우는 환경변수로 표현된다.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CommonSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_", extra="ignore")

    block_id: str = "unknown"
    """카탈로그 블록 ID. 로그·메트릭 라벨과 라이선스 게이팅에 쓰인다."""

    environment: str = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    redis_url: str = "redis://localhost:6379/0"
    consumer_group: str = "default"
    consumer_name: str = "worker-1"

    license_path: str | None = None
    """``.lic`` 경로. 미지정 시 개발 모드로 모든 블록을 허용한다."""

    core_bus_url: str = "http://localhost:8081"
    """CORE-BUS 내부 API. 블록은 세션 정보를 여기서만 조회한다."""


@lru_cache
def get_settings() -> CommonSettings:
    return CommonSettings()
