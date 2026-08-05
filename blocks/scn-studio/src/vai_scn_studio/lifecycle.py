"""시나리오 생애주기 · 반출입 · 학습 자료 변환.

경쟁 제안서가 그림으로 보여 주는 것 중 우리에게 없던 것이 두 가지였다:
**시나리오 Life Cycle** 과 **Export/Import**. 둘은 같은 논리를 떠받친다 —
"고객사가 자체적으로 시나리오를 늘려 간다". 그게 안 되면 안내 문구 한 줄에도
공급사가 나가야 하고, 그 출동 원가가 라이선스 수익을 잠식한다.

세 가지 규칙이 이 모듈의 전부다.

1. **한 칸씩만 올라간다.** 편집 직후 운영으로 가는 지름길을 열면 결국 그 길로만
   다니게 되고, 검증과 시연은 형식이 된다.
2. **편집하면 DRAFT로 내려간다.** 검증을 통과한 뒤 한 글자만 고쳐도 그것은 더
   이상 검증된 물건이 아니다. 이 한 줄이 없으면 생애주기 전체가 장식이 된다.
3. **되돌릴 수 있다.** 배포 이력을 남기고, 과거 판을 초안으로 되살린다.
"""

from __future__ import annotations

import csv
import io
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from redis.asyncio import Redis

from vai_contracts.dialog import (
    STAGE_ORDER,
    DialogNode,
    Intent,
    NodeKind,
    Scenario,
    ScenarioRevision,
    ScenarioStage,
)

DRAFT_KEY = "vai:authoring:scenario:draft:"
HISTORY_KEY = "vai:authoring:scenario:history:"
HISTORY_LIMIT = 50

CSV_HEADER = ["node_id", "node_kind", "prompt", "intent", "next_node", "patterns", "examples"]
"""학습 도구 열 구성.

**엑셀이 그대로 여는 CSV** 를 쓴다. `.xlsx` 를 만들려면 라이브러리가 하나 더
필요하고, 폐쇄망 반입 목록이 그만큼 길어진다. 얻는 것은 셀 서식뿐이다.
"""


class StageError(ValueError):
    """생애주기를 건너뛰려 했다."""


class ScenarioStore(ABC):
    @abstractmethod
    async def get(self, tenant_id: str) -> ScenarioRevision | None: ...

    @abstractmethod
    async def put(self, tenant_id: str, revision: ScenarioRevision) -> None: ...

    @abstractmethod
    async def push_history(self, tenant_id: str, revision: ScenarioRevision) -> None: ...

    @abstractmethod
    async def history(self, tenant_id: str) -> list[ScenarioRevision]: ...


class RedisScenarioStore(ScenarioStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, tenant_id: str) -> ScenarioRevision | None:
        raw = await self._redis.get(DRAFT_KEY + tenant_id)
        return ScenarioRevision.model_validate_json(raw) if raw else None

    async def put(self, tenant_id: str, revision: ScenarioRevision) -> None:
        await self._redis.set(DRAFT_KEY + tenant_id, revision.model_dump_json().encode())

    async def push_history(self, tenant_id: str, revision: ScenarioRevision) -> None:
        key = HISTORY_KEY + tenant_id
        await self._redis.lpush(key, revision.model_dump_json().encode())
        await self._redis.ltrim(key, 0, HISTORY_LIMIT - 1)

    async def history(self, tenant_id: str) -> list[ScenarioRevision]:
        rows = await self._redis.lrange(HISTORY_KEY + tenant_id, 0, HISTORY_LIMIT - 1)
        return [ScenarioRevision.model_validate_json(row) for row in rows]


class InMemoryScenarioStore(ScenarioStore):
    def __init__(self) -> None:
        self._drafts: dict[str, ScenarioRevision] = {}
        self._history: dict[str, list[ScenarioRevision]] = {}

    async def get(self, tenant_id: str) -> ScenarioRevision | None:
        found = self._drafts.get(tenant_id)
        return found.model_copy(deep=True) if found else None

    async def put(self, tenant_id: str, revision: ScenarioRevision) -> None:
        self._drafts[tenant_id] = revision.model_copy(deep=True)

    async def push_history(self, tenant_id: str, revision: ScenarioRevision) -> None:
        items = self._history.setdefault(tenant_id, [])
        items.insert(0, revision.model_copy(deep=True))
        del items[HISTORY_LIMIT:]

    async def history(self, tenant_id: str) -> list[ScenarioRevision]:
        return [item.model_copy(deep=True) for item in self._history.get(tenant_id, [])]


def next_stage(current: ScenarioStage) -> ScenarioStage:
    index = STAGE_ORDER.index(current)
    if index + 1 >= len(STAGE_ORDER):
        raise StageError("이미 운영 단계다")
    return STAGE_ORDER[index + 1]


def check_promotion(current: ScenarioStage, wanted: ScenarioStage) -> None:
    """건너뛰기를 막는다.

    DRAFT에서 곧장 LIVE로 가는 길이 있으면 결국 모두가 그 길로 간다. 그러면
    막다른 골목이 있는 시나리오가 운영에 올라가고, 그 사실은 고객이 갇힌
    뒤에야 드러난다.
    """
    if wanted == current:
        raise StageError(f"이미 {current.value} 단계다")
    if STAGE_ORDER.index(wanted) != STAGE_ORDER.index(current) + 1:
        raise StageError(
            f"{current.value} → {wanted.value} 로 건너뛸 수 없다 — "
            f"다음 단계는 {next_stage(current).value} 다"
        )


def touch_draft(revision: ScenarioRevision, *, note: str = "", by: str = "") -> ScenarioRevision:
    """편집 반영. **단계를 DRAFT로 되돌린다.**

    검증을 통과한 뒤 한 글자만 고쳐도 그 시나리오는 더 이상 검증된 물건이
    아니다. 이 되돌림이 없으면 생애주기 표시는 장식이 된다.
    """
    revision.stage = ScenarioStage.DRAFT
    revision.problems = []
    revision.note = note or revision.note
    revision.updated_by = by or revision.updated_by
    revision.updated_at = datetime.now(UTC)
    return revision


# ── 반출입 ───────────────────────────────────────────────────────────────────


def export_bundle(revision: ScenarioRevision) -> dict[str, object]:
    """반출 꾸러미.

    단계는 싣지 않는다. 다른 환경에 들여온 시나리오가 '운영'으로 표시되면
    검증 없이 배포로 이어진다 — 반입본은 언제나 초안에서 시작한다.
    """
    return {
        "format": "visionai.scenario/v1",
        "exported_at": datetime.now(UTC).isoformat(),
        "note": revision.note,
        "scenario": revision.scenario.model_dump(mode="json"),
    }


def import_bundle(payload: dict[str, object], *, tenant_id: str) -> Scenario:
    """반입. **테넌트는 받는 쪽 것으로 덮어쓴다.**

    꾸러미 안의 테넌트를 그대로 믿으면, 남의 시나리오 파일 하나로 다른 고객사
    영역에 쓰기가 일어난다.
    """
    body = payload.get("scenario")
    if not isinstance(body, dict):
        raise ValueError("scenario 항목이 없다 — 반출 꾸러미가 맞는지 확인한다")
    fmt = str(payload.get("format", ""))
    if fmt and not fmt.startswith("visionai.scenario/"):
        raise ValueError(f"모르는 형식이다: {fmt}")
    scenario = Scenario.model_validate(body)
    scenario.tenant_id = tenant_id
    scenario.published = False
    return scenario


def clone_flow(scenario: Scenario, node_ids: list[str], *, suffix: str) -> Scenario:
    """노드 묶음 복제 — 비슷한 흐름을 처음부터 다시 쓰지 않게 한다.

    복제한 노드들 **안쪽으로 향하는 연결만** 새 이름으로 바꾼다. 바깥으로
    나가는 연결은 그대로 두어야 복제본이 기존 종료·연결 지점을 공유한다.
    """
    if not suffix:
        raise ValueError("복제본 접미사가 필요하다 — 없으면 노드 ID가 충돌한다")
    wanted = [node for node in scenario.nodes if node.node_id in set(node_ids)]
    if not wanted:
        raise ValueError("복제할 노드를 찾지 못했다")

    inside = {node.node_id for node in wanted}
    rename = {node_id: f"{node_id}{suffix}" for node_id in inside}
    existing = {node.node_id for node in scenario.nodes}
    collisions = set(rename.values()) & existing
    if collisions:
        raise ValueError(f"이미 있는 노드 ID다: {', '.join(sorted(collisions))}")

    copies = []
    for node in wanted:
        copy = node.model_copy(deep=True)
        copy.node_id = rename[node.node_id]
        copy.next_node = rename.get(copy.next_node, copy.next_node)
        for intent in copy.intents:
            intent.next_node = rename.get(intent.next_node, intent.next_node)
        copies.append(copy)

    cloned = scenario.model_copy(deep=True)
    cloned.nodes = [*cloned.nodes, *copies]
    return cloned


# ── 학습 도구 (인텐트 일괄 편집) ─────────────────────────────────────────────


def intents_to_csv(scenario: Scenario) -> str:
    """인텐트를 표로 내린다.

    BOM을 붙인다. 없으면 엑셀이 한글을 깨서 열고, 한 번 깨져 본 사람은 그
    도구를 다시 쓰지 않는다.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for node in scenario.nodes:
        if not node.intents:
            # 인텐트가 없는 노드도 한 줄 남긴다. 표만 보고 흐름 전체를 알 수
            # 있어야 하고, 여기가 비면 "빠뜨린 것"과 "원래 없는 것"이 같아 보인다.
            writer.writerow(
                [node.node_id, node.kind.value, node.prompt, "", node.next_node, "", ""]
            )
            continue
        for intent in node.intents:
            writer.writerow(
                [
                    node.node_id,
                    node.kind.value,
                    node.prompt,
                    intent.name,
                    intent.next_node,
                    "|".join(intent.patterns),
                    "|".join(intent.examples),
                ]
            )
    return "﻿" + buffer.getvalue()


def intents_from_csv(text: str, *, scenario: Scenario) -> Scenario:
    """표에서 인텐트를 되받는다. **노드는 새로 만들지 않는다.**

    표 편집으로 흐름 구조까지 바뀌면 되돌리기 어려운 사고가 난다 — 오타 하나로
    빈 노드가 생기고, 그 노드는 아무 데도 연결되지 않은 채 검증만 통과한다.
    여기서는 이미 있는 노드의 인텐트만 갈아 끼운다.
    """
    stripped = text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(stripped)))
    if not rows:
        raise ValueError("빈 표다")
    missing = [column for column in ("node_id", "intent") if column not in (rows[0] or {})]
    if missing:
        raise ValueError(f"열이 없다: {', '.join(missing)} — 내려받은 양식을 그대로 쓴다")

    updated = scenario.model_copy(deep=True)
    known = {node.node_id: node for node in updated.nodes}
    unknown: set[str] = set()
    collected: dict[str, list[Intent]] = {}

    for row in rows:
        node_id = (row.get("node_id") or "").strip()
        if not node_id:
            continue
        if node_id not in known:
            unknown.add(node_id)
            continue
        name = (row.get("intent") or "").strip()
        if not name:
            collected.setdefault(node_id, [])
            continue
        collected.setdefault(node_id, []).append(
            Intent(
                name=name,
                next_node=(row.get("next_node") or "").strip(),
                patterns=_split(row.get("patterns")),
                examples=_split(row.get("examples")),
            )
        )

    if unknown:
        raise ValueError(
            f"시나리오에 없는 노드다: {', '.join(sorted(unknown))} — "
            "노드는 저작 화면에서 먼저 만든다"
        )

    for node_id, intents in collected.items():
        known[node_id].intents = intents
    return updated


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def blank_scenario(tenant_id: str) -> Scenario:
    """새 시나리오의 출발점.

    ``handoff`` 노드를 처음부터 넣는다. 검증이 "사람 연결에 도달 가능"을
    요구하므로, 빈 시나리오가 곧바로 검증 실패로 나오면 저작자는 도구가
    고장 났다고 생각한다.
    """
    return Scenario(
        scenario_id=f"{tenant_id}-main",
        tenant_id=tenant_id,
        entry_node="start",
        fallback_node="handoff",
        nodes=[
            DialogNode(
                node_id="start",
                kind=NodeKind.SAY,
                prompt="안녕하세요. 무엇을 도와드릴까요?",
                next_node="handoff",
            ),
            DialogNode(
                node_id="handoff",
                kind=NodeKind.HANDOFF,
                prompt="상담원을 연결해 드리겠습니다.",
            ),
        ],
    )
