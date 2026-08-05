"""저작 산출물 저장소 — 룰셋·사전·평가셋.

핵심은 **초안과 배포본의 분리**다. 운영자가 룰을 고치는 즉시 상담 파이프라인이
바뀌면, 정규식 하나 잘못 쓴 순간 전 채널의 상담 로그가 오염된다. 저작은 초안에
하고, 명시적으로 배포(publish)해야 반영된다. 되돌릴 수 있도록 버전을 남긴다.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from redis.asyncio import Redis

from vai_contracts.authoring import Lexicon, RuleSet

DRAFT_PREFIX = "vai:authoring:draft:"
PUBLISHED_PREFIX = "vai:authoring:published:"
HISTORY_PREFIX = "vai:authoring:history:"
HISTORY_LIMIT = 50
"""보관할 과거 배포본 수. 되돌리기는 최근 것만 필요하고, 감사 이력은
CORE-SEC의 감사 로그가 따로 남긴다."""


class AuthoringStore(ABC):
    """룰셋·사전의 초안/배포본 관리."""

    @abstractmethod
    async def get_rules(self, tenant_id: str, *, published: bool) -> RuleSet | None: ...

    @abstractmethod
    async def save_draft_rules(self, ruleset: RuleSet) -> RuleSet: ...

    @abstractmethod
    async def publish_rules(self, tenant_id: str) -> RuleSet | None: ...

    @abstractmethod
    async def rule_history(self, tenant_id: str) -> list[RuleSet]: ...

    @abstractmethod
    async def get_lexicon(self, tenant_id: str) -> Lexicon | None: ...

    @abstractmethod
    async def save_lexicon(self, lexicon: Lexicon) -> Lexicon: ...


class RedisAuthoringStore(AuthoringStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _rules_key(tenant_id: str, *, published: bool) -> str:
        prefix = PUBLISHED_PREFIX if published else DRAFT_PREFIX
        return f"{prefix}rules:{tenant_id}"

    async def get_rules(self, tenant_id: str, *, published: bool) -> RuleSet | None:
        raw = await self._redis.get(self._rules_key(tenant_id, published=published))
        return RuleSet.model_validate_json(raw) if raw else None

    async def save_draft_rules(self, ruleset: RuleSet) -> RuleSet:
        ruleset.published = False
        ruleset.updated_at = datetime.now(UTC)
        await self._redis.set(
            self._rules_key(ruleset.tenant_id, published=False), ruleset.model_dump_json().encode()
        )
        return ruleset

    async def publish_rules(self, tenant_id: str) -> RuleSet | None:
        draft = await self.get_rules(tenant_id, published=False)
        if draft is None:
            return None

        current = await self.get_rules(tenant_id, published=True)
        if current is not None:
            # 되돌릴 수 있게 직전 배포본을 이력에 밀어 넣는다.
            await self._redis.lpush(HISTORY_PREFIX + tenant_id, current.model_dump_json().encode())
            await self._redis.ltrim(HISTORY_PREFIX + tenant_id, 0, HISTORY_LIMIT - 1)

        draft.version = (current.version + 1) if current else 1
        draft.published = True
        draft.updated_at = datetime.now(UTC)
        await self._redis.set(
            self._rules_key(tenant_id, published=True), draft.model_dump_json().encode()
        )
        # 초안도 같은 버전으로 맞춰 둔다. 다음 편집은 이 위에서 시작한다.
        await self._redis.set(
            self._rules_key(tenant_id, published=False),
            draft.model_copy(update={"published": False}).model_dump_json().encode(),
        )
        return draft

    async def rule_history(self, tenant_id: str) -> list[RuleSet]:
        raw_items = await self._redis.lrange(HISTORY_PREFIX + tenant_id, 0, HISTORY_LIMIT - 1)
        return [RuleSet.model_validate_json(item) for item in raw_items]

    async def get_lexicon(self, tenant_id: str) -> Lexicon | None:
        raw = await self._redis.get(f"{PUBLISHED_PREFIX}lexicon:{tenant_id}")
        return Lexicon.model_validate_json(raw) if raw else None

    async def save_lexicon(self, lexicon: Lexicon) -> Lexicon:
        current = await self.get_lexicon(lexicon.tenant_id)
        lexicon.version = (current.version + 1) if current else 1
        lexicon.updated_at = datetime.now(UTC)
        await self._redis.set(
            f"{PUBLISHED_PREFIX}lexicon:{lexicon.tenant_id}", lexicon.model_dump_json().encode()
        )
        return lexicon


class InMemoryAuthoringStore(AuthoringStore):
    """테스트·단일 프로세스용."""

    def __init__(self) -> None:
        self._drafts: dict[str, RuleSet] = {}
        self._published: dict[str, RuleSet] = {}
        self._history: dict[str, list[RuleSet]] = {}
        self._lexicons: dict[str, Lexicon] = {}

    async def get_rules(self, tenant_id: str, *, published: bool) -> RuleSet | None:
        source = self._published if published else self._drafts
        found = source.get(tenant_id)
        return found.model_copy(deep=True) if found else None

    async def save_draft_rules(self, ruleset: RuleSet) -> RuleSet:
        ruleset.published = False
        ruleset.updated_at = datetime.now(UTC)
        self._drafts[ruleset.tenant_id] = ruleset.model_copy(deep=True)
        return ruleset

    async def publish_rules(self, tenant_id: str) -> RuleSet | None:
        draft = self._drafts.get(tenant_id)
        if draft is None:
            return None
        current = self._published.get(tenant_id)
        if current is not None:
            self._history.setdefault(tenant_id, []).insert(0, current.model_copy(deep=True))
            del self._history[tenant_id][HISTORY_LIMIT:]

        published = draft.model_copy(deep=True)
        published.version = (current.version + 1) if current else 1
        published.published = True
        published.updated_at = datetime.now(UTC)
        self._published[tenant_id] = published
        self._drafts[tenant_id] = published.model_copy(update={"published": False}, deep=True)
        return published

    async def rule_history(self, tenant_id: str) -> list[RuleSet]:
        return [item.model_copy(deep=True) for item in self._history.get(tenant_id, [])]

    async def get_lexicon(self, tenant_id: str) -> Lexicon | None:
        found = self._lexicons.get(tenant_id)
        return found.model_copy(deep=True) if found else None

    async def save_lexicon(self, lexicon: Lexicon) -> Lexicon:
        current = self._lexicons.get(lexicon.tenant_id)
        lexicon.version = (current.version + 1) if current else 1
        lexicon.updated_at = datetime.now(UTC)
        self._lexicons[lexicon.tenant_id] = lexicon.model_copy(deep=True)
        return lexicon


def export_rules_for_pipeline(ruleset: RuleSet) -> str:
    """배포본을 FLT-MICRO가 읽는 형식으로 직렬화한다.

    배포는 설정 채널로 자동 반영되므로 이 형식이 주 경로는 아니다. 채널을 쓰지
    않는 구성(파일 주입)과 형상 검토·백업을 위해 같은 형식을 유지한다.
    """
    return json.dumps(
        {
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "pattern": rule.pattern,
                    "severity": rule.severity.value,
                    "message": rule.message,
                }
                for rule in ruleset.rules
                if rule.enabled
            ]
        },
        ensure_ascii=False,
        indent=2,
    )
