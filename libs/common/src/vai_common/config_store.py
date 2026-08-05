"""배포 설정 채널 — 저작 블록과 실시간 블록을 잇는 단방향 통로.

문제: STT-CORE가 커스텀 사전을 쓰려면 SCN-STUDIO가 저작한 내용을 알아야 한다.
그렇다고 실시간 블록이 저작 도구를 HTTP로 부르면, 저작 도구가 죽었을 때
상담이 멈춘다. 저작 도구는 업무시간에만 켜 두는 고객사도 있다.

해결: 저작 블록은 **배포된 설정을 공유 저장소에 쓰고**, 실시간 블록은 **읽기만**
한다. 둘은 서로를 모르며, 저작 도구가 꺼져 있어도 마지막 배포본으로 계속 돈다.

같은 통로로 Wave 6의 룰셋 자동 배포도 처리한다 — 지금은 사전만 쓴다.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis

log = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

KEY_PREFIX = "vai:config:"
VERSION_CHANNEL = "vai:config-changed"
"""배포 시 발행되는 Pub/Sub 채널. 실시간 블록이 폴링 없이 갱신을 알아챈다."""


class ConfigKind(StrEnum):
    """배포 가능한 설정 종류."""

    LEXICON = "lexicon"
    """STT 커스텀 사전."""

    RULESET = "ruleset"
    """컴플라이언스 룰셋 (Wave 6에서 활성화)."""


def config_key(kind: ConfigKind, tenant_id: str) -> str:
    return f"{KEY_PREFIX}{kind.value}:{tenant_id}"


class ConfigStore(ABC):
    @abstractmethod
    async def publish(self, kind: ConfigKind, tenant_id: str, config: BaseModel) -> None:
        """설정을 배포한다. 저작 블록만 호출한다."""

    @abstractmethod
    async def load(self, kind: ConfigKind, tenant_id: str, model: type[ModelT]) -> ModelT | None:
        """배포된 설정을 읽는다. 실시간 블록이 호출한다."""


class RedisConfigStore(ConfigStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, kind: ConfigKind, tenant_id: str, config: BaseModel) -> None:
        await self._redis.set(config_key(kind, tenant_id), config.model_dump_json().encode())
        # 실시간 블록이 캐시를 비울 수 있게 알린다. 놓쳐도 TTL 갱신으로 따라잡으므로
        # 전달 보장은 필요 없다.
        await self._redis.publish(VERSION_CHANNEL, f"{kind.value}:{tenant_id}".encode())
        log.info("설정 배포", extra={"kind": kind.value, "tenant_id": tenant_id})

    async def load(self, kind: ConfigKind, tenant_id: str, model: type[ModelT]) -> ModelT | None:
        raw = await self._redis.get(config_key(kind, tenant_id))
        if raw is None:
            return None
        try:
            return model.model_validate_json(raw)
        except ValidationError:
            # 저작 블록이 새 스키마로 쓴 설정을 구 버전 실시간 블록이 읽는 경우.
            # 여기서 죽으면 상담이 멈추므로, 무시하고 이전 동작을 유지한다.
            log.exception(
                "배포 설정 파싱 실패 — 무시하고 계속한다",
                extra={"kind": kind.value, "tenant_id": tenant_id},
            )
            return None


class InMemoryConfigStore(ConfigStore):
    """테스트·단일 프로세스용."""

    def __init__(self) -> None:
        self._data: dict[str, BaseModel] = {}

    async def publish(self, kind: ConfigKind, tenant_id: str, config: BaseModel) -> None:
        self._data[config_key(kind, tenant_id)] = config.model_copy(deep=True)

    async def load(self, kind: ConfigKind, tenant_id: str, model: type[ModelT]) -> ModelT | None:
        found = self._data.get(config_key(kind, tenant_id))
        return model.model_validate(found.model_dump()) if found else None


class CachedConfig(Generic[ModelT]):
    """테넌트별 설정 캐시.

    실시간 경로가 발화마다 Redis를 때리면 지연 예산을 캐시 미스에 쓰게 된다.
    TTL을 두고 그 안에서는 메모리에서 답한다. 배포 직후 최대 TTL만큼 옛 설정이
    쓰이지만, 사전·룰 배포는 즉시성이 요구되는 작업이 아니다.
    """

    def __init__(
        self, store: ConfigStore, kind: ConfigKind, model: type[ModelT], *, ttl_s: float = 30.0
    ) -> None:
        self._store = store
        self._kind = kind
        self._model = model
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, ModelT | None]] = {}

    async def get(self, tenant_id: str) -> ModelT | None:
        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        loaded = await self._store.load(self._kind, tenant_id, self._model)
        self._cache[tenant_id] = (now, loaded)
        return loaded

    def invalidate(self, tenant_id: str | None = None) -> None:
        """배포 알림을 받았을 때 호출. 인자가 없으면 전체를 비운다."""
        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)
