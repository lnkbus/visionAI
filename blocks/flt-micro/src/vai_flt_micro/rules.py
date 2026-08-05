"""테넌트별 룰셋 캐시 — 저작 콘솔의 배포를 실제 상담에 반영한다.

배포 채널이 닫히기 전까지 컴플라이언스 룰 "배포"는 운영자가 콘솔에서 내려받은
JSON을 파일에 붙여 넣고 블록을 재기동하는 일이었다. **고객사가 룰 한 줄 바꾸는 데
공급사 인력이 필요하면 유지보수 원가가 라이선스 수익을 잠식한다** — 저작 도구를
만든 이유가 그것인데, 정작 마지막 한 걸음이 수작업이었다.

여기서 지키는 것:

* **정규식 컴파일은 테넌트·버전당 한 번.** Fast-Path 예산이 10ms다. 발화마다
  컴파일하면 그 예산을 컴파일에 쓴다.
* **배포된 룰이 깨져도 마스킹은 계속된다.** 룰 컴파일 실패로 필터 전체가 죽으면
  PII가 마스킹되지 않은 채 흐른다 — 룰 문제로 유출 사고를 만들 수는 없다.
  깨진 룰셋은 버리고 직전에 쓰던 필터로 계속 간다.
* **배포본이 없으면 파일 주입본을 쓴다.** 채널을 닫았다고 기존 배포 방식이
  갑자기 무효가 되면 운영 중인 현장이 멈춘다.
"""

from __future__ import annotations

import logging
import re

from vai_common.config_store import CachedConfig
from vai_contracts.authoring import RuleSet
from vai_flt_micro.filter import ComplianceRule, MicroComplianceFilter

log = logging.getLogger(__name__)


class RuleSetCache:
    """테넌트 → 컴파일된 필터.

    :class:`~vai_common.config_store.CachedConfig`가 조회를 캐시하고, 이 클래스는
    **컴파일 결과**를 캐시한다. 둘은 다른 비용이다 — 조회는 네트워크, 컴파일은 CPU.
    """

    def __init__(self, configs: CachedConfig[RuleSet], fallback: MicroComplianceFilter) -> None:
        self._configs = configs
        self._fallback = fallback
        """배포본이 없거나 깨졌을 때 쓸 필터(파일 주입본). PII 마스킹은 여기에도 있다."""

        self._compiled: dict[str, tuple[int, MicroComplianceFilter]] = {}

    async def filter_for(self, tenant_id: str) -> MicroComplianceFilter:
        ruleset = await self._configs.get(tenant_id)
        if ruleset is None or not ruleset.published:
            return self._fallback

        cached = self._compiled.get(tenant_id)
        if cached is not None and cached[0] == ruleset.version:
            return cached[1]

        compiled = self._compile(ruleset)
        if compiled is None:
            # 컴파일 실패. 직전 것이 있으면 그걸 쓰고, 없으면 파일 주입본으로 간다.
            return cached[1] if cached else self._fallback

        self._compiled[tenant_id] = (ruleset.version, compiled)
        log.info(
            "배포된 룰셋 적용",
            extra={
                "tenant_id": tenant_id,
                "version": ruleset.version,
                "rule_count": compiled.rule_count,
            },
        )
        return compiled

    def _compile(self, ruleset: RuleSet) -> MicroComplianceFilter | None:
        rules: list[ComplianceRule] = []
        for spec in ruleset.rules:
            if not spec.enabled:
                continue
            try:
                rules.append(
                    ComplianceRule(
                        rule_id=spec.rule_id,
                        pattern=re.compile(spec.pattern),
                        severity=spec.severity.value,
                        message=spec.message,
                    )
                )
            except re.error:
                # 한 룰이 깨졌다고 나머지를 버리지 않는다. 다만 조용히 넘기지도
                # 않는다 — 이 로그가 저작자에게 돌아갈 신호다.
                log.warning(
                    "룰 정규식 컴파일 실패 — 이 룰만 제외한다",
                    extra={"tenant_id": ruleset.tenant_id, "rule_id": spec.rule_id},
                )
        if not rules and ruleset.rules:
            log.error(
                "배포된 룰셋의 모든 룰이 컴파일에 실패했다 — 직전 설정을 유지한다",
                extra={"tenant_id": ruleset.tenant_id, "version": ruleset.version},
            )
            return None
        return MicroComplianceFilter(rules)

    def invalidate(self, tenant_id: str | None = None) -> None:
        """배포 알림을 받았을 때 호출. 조회 캐시와 컴파일 캐시를 함께 비운다."""
        self._configs.invalidate(tenant_id)
        if tenant_id is None:
            self._compiled.clear()
        else:
            self._compiled.pop(tenant_id, None)
