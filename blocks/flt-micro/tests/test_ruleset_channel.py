"""배포된 룰셋이 실제 상담에 반영되는지.

채널이 닫히기 전까지 "배포"는 운영자가 JSON을 파일에 붙여 넣고 블록을
재기동하는 일이었다. 저작 도구를 만든 이유가 유지보수 원가인데, 정작 마지막
한 걸음이 사람 손이면 그 이유가 사라진다.

여기서 함께 지키는 것: **룰이 잘못돼도 PII 마스킹은 멈추지 않는다.** 룰은
컴플라이언스 보조이고 마스킹은 유출 방지다. 전자의 실패로 후자를 잃을 수 없다.
"""

from __future__ import annotations

import pytest

from vai_common.config_store import CachedConfig, ConfigKind, InMemoryConfigStore
from vai_contracts.authoring import ComplianceRuleSpec, RuleSet, RuleSeverity
from vai_flt_micro.filter import MicroComplianceFilter
from vai_flt_micro.rules import RuleSetCache

TENANT = "acme"


def _ruleset(version: int, *specs: ComplianceRuleSpec, published: bool = True) -> RuleSet:
    return RuleSet(tenant_id=TENANT, version=version, rules=list(specs), published=published)


def _spec(rule_id: str, pattern: str, enabled: bool = True) -> ComplianceRuleSpec:
    return ComplianceRuleSpec(
        rule_id=rule_id,
        pattern=pattern,
        severity=RuleSeverity.VIOLATION,
        message=f"{rule_id} 위반",
        enabled=enabled,
    )


@pytest.fixture
def cache() -> tuple[RuleSetCache, InMemoryConfigStore]:
    store = InMemoryConfigStore()
    fallback = MicroComplianceFilter([])
    cached = CachedConfig(store, ConfigKind.RULESET, RuleSet, ttl_s=0.0)
    return RuleSetCache(cached, fallback), store


async def test_published_ruleset_reaches_the_pipeline(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    rulesets, store = cache
    await store.publish(
        ConfigKind.RULESET, TENANT, _ruleset(1, _spec("no-guarantee", "반드시 수익"))
    )

    active = await rulesets.filter_for(TENANT)
    outcome = active.process_text("이 상품은 반드시 수익이 납니다")

    assert [rule.rule_id for rule in outcome.matched_rules] == ["no-guarantee"]


async def test_unpublished_draft_is_ignored(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    """편집 중인 정규식이 상담 파이프라인에 새어 나가면 안 된다."""
    rulesets, store = cache
    await store.publish(
        ConfigKind.RULESET, TENANT, _ruleset(1, _spec("draft", "아무거나"), published=False)
    )

    active = await rulesets.filter_for(TENANT)

    assert active.rule_count == 0


async def test_tenant_without_deployment_uses_the_file_default(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    """채널을 닫았다고 기존 파일 주입 구성이 갑자기 무효가 되면 현장이 멈춘다."""
    rulesets, _ = cache
    assert (await rulesets.filter_for("배포한적없는테넌트")).rule_count == 0


async def test_new_version_replaces_the_old(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    rulesets, store = cache
    await store.publish(ConfigKind.RULESET, TENANT, _ruleset(1, _spec("old", "옛 문구")))
    assert (await rulesets.filter_for(TENANT)).rule_count == 1

    updated = _ruleset(2, _spec("new-a", "새 문구"), _spec("new-b", "또 다른"))
    await store.publish(ConfigKind.RULESET, TENANT, updated)
    rulesets.invalidate(TENANT)

    active = await rulesets.filter_for(TENANT)
    assert active.rule_count == 2
    assert not active.process_text("옛 문구가 있다").matched_rules


async def test_disabled_rules_are_not_compiled(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    rulesets, store = cache
    await store.publish(
        ConfigKind.RULESET,
        TENANT,
        _ruleset(1, _spec("on", "켜짐"), _spec("off", "꺼짐", enabled=False)),
    )

    assert (await rulesets.filter_for(TENANT)).rule_count == 1


async def test_broken_regex_drops_only_that_rule(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    rulesets, store = cache
    await store.publish(
        ConfigKind.RULESET, TENANT, _ruleset(1, _spec("ok", "정상"), _spec("bad", "([미완성"))
    )

    active = await rulesets.filter_for(TENANT)

    assert active.rule_count == 1
    assert active.process_text("정상 문구").matched_rules


async def test_masking_survives_a_completely_broken_ruleset(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    """룰 문제로 유출 사고를 만들 수는 없다."""
    rulesets, store = cache
    await store.publish(ConfigKind.RULESET, TENANT, _ruleset(1, _spec("bad", "([미완성")))

    active = await rulesets.filter_for(TENANT)
    outcome = active.process_text("주민번호는 900101-1234567 입니다")

    assert outcome.pii_masked
    assert "900101-1234567" not in outcome.clean_text


async def test_compiled_filter_is_reused_within_a_version(
    cache: tuple[RuleSetCache, InMemoryConfigStore],
) -> None:
    """Fast-Path 예산이 10ms다. 발화마다 정규식을 다시 컴파일하면 그 예산을 잃는다."""
    rulesets, store = cache
    await store.publish(ConfigKind.RULESET, TENANT, _ruleset(1, _spec("r", "문구")))

    first = await rulesets.filter_for(TENANT)
    second = await rulesets.filter_for(TENANT)

    assert first is second
