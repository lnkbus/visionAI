"""저작 스튜디오 계약 테스트.

가장 중요한 불변식: **초안 편집이 상담 파이프라인에 새어 나가지 않는다.**
정규식 하나 잘못 쓴 순간 전 채널 로그가 오염되므로, 배포는 명시적 행위여야 한다.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from vai_contracts.authoring import PopupFeedback
from vai_scn_studio.app import create_app
from vai_scn_studio.feedback import InMemoryFeedbackStore
from vai_scn_studio.store import InMemoryAuthoringStore, export_rules_for_pipeline
from vai_scn_studio.validation import validate_pattern

TENANT = "t1"

RULE = {
    "rule_id": "FORBIDDEN_GUARANTEE",
    "pattern": "원금.?보장",
    "severity": "violation",
    "message": "확정 수익 표현은 금지",
    "enabled": True,
}


def filter_stub() -> httpx.AsyncClient:
    """FLT-MICRO 대역. 실제 블록은 스택 테스트가 검증한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        import re

        body = json.loads(request.content)
        text = body["text"]
        masked = re.sub(r"\d{6}-[1-4]\d{6}", "[RRN_MASKED]", text)
        matched = [
            rule["rule_id"] for rule in body.get("rules") or [] if re.search(rule["pattern"], text)
        ]
        return httpx.Response(
            200,
            json={
                "clean_text": masked,
                "pii_masked": masked != text,
                "pii_types": ["rrn"] if masked != text else [],
                "matched_rules": matched,
                "elapsed_ms": 0.4,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://flt")


def search_stub(hits: list[dict] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": hits or [],
                "latency_ms": 12,
                "dense_ms": 5,
                "sparse_ms": 3,
                "rerank_ms": 4,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://srch")


@pytest.fixture
def store() -> InMemoryAuthoringStore:
    return InMemoryAuthoringStore()


@pytest.fixture
async def client(store: InMemoryAuthoringStore):
    app = create_app(
        store=store,
        feedback=InMemoryFeedbackStore(),
        search_client=search_stub(
            [
                {
                    "chunk": {
                        "chunk_id": "c0",
                        "doc_id": "doc_card_policy_012",
                        "tenant_id": TENANT,
                        "kb_id": "default",
                        "title": "결제일 연기 규정",
                        "text": "최대 5일까지",
                    },
                    "score": 0.9,
                    "dense_score": 0.8,
                    "sparse_score": 3.1,
                    "rerank_score": 0.9,
                }
            ]
        ),
        filter_client=filter_stub(),
    )
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://studio") as http,
        app.router.lifespan_context(app),
    ):
        yield http


# ── 룰 저작 ──────────────────────────────────────────────────────────────────


async def test_draft_does_not_reach_the_pipeline_until_published(
    client: httpx.AsyncClient, store: InMemoryAuthoringStore
) -> None:
    """저작 도구의 존재 이유. 편집이 즉시 반영되면 도구가 아니라 사고 원인이다."""
    await client.put(f"/internal/v1/rules/{TENANT}", json={"tenant_id": TENANT, "rules": [RULE]})

    assert await store.get_rules(TENANT, published=True) is None, "배포 전에는 배포본이 없어야 한다"

    await client.post(f"/internal/v1/rules/{TENANT}/publish")
    published = await store.get_rules(TENANT, published=True)

    assert published is not None
    assert [r.rule_id for r in published.rules] == ["FORBIDDEN_GUARANTEE"]


async def test_publish_bumps_version_and_keeps_history(client: httpx.AsyncClient) -> None:
    """되돌릴 수 없는 배포는 운영자가 무서워서 못 쓴다."""
    await client.put(f"/internal/v1/rules/{TENANT}", json={"tenant_id": TENANT, "rules": [RULE]})
    first = (await client.post(f"/internal/v1/rules/{TENANT}/publish")).json()

    changed = {**RULE, "message": "수정됨"}
    await client.put(f"/internal/v1/rules/{TENANT}", json={"tenant_id": TENANT, "rules": [changed]})
    second = (await client.post(f"/internal/v1/rules/{TENANT}/publish")).json()

    history = (await client.get(f"/internal/v1/rules/{TENANT}/history")).json()

    assert first["ruleset"]["version"] == 1
    assert second["ruleset"]["version"] == 2
    assert len(history) == 1
    assert history[0]["version"] == 1


async def test_invalid_regex_is_refused_at_authoring_time(client: httpx.AsyncClient) -> None:
    """잘못된 정규식이 배포되면 필터 블록이 기동에 실패한다. 여기서 막아야 한다."""
    response = await client.put(
        f"/internal/v1/rules/{TENANT}",
        json={"tenant_id": TENANT, "rules": [{**RULE, "pattern": "([unclosed"}]},
    )

    assert response.status_code == 400
    assert "정규식" in response.json()["detail"]


async def test_duplicate_rule_ids_are_refused(client: httpx.AsyncClient) -> None:
    """룰 ID가 겹치면 어느 룰이 걸렸는지 로그에서 구분할 수 없다."""
    response = await client.put(
        f"/internal/v1/rules/{TENANT}", json={"tenant_id": TENANT, "rules": [RULE, RULE]}
    )

    assert response.status_code == 400
    assert "중복" in response.json()["detail"]


async def test_publishing_without_a_draft_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.post(f"/internal/v1/rules/{TENANT}/publish")).status_code == 404


async def test_rule_test_uses_the_real_filter_block(client: httpx.AsyncClient) -> None:
    """자체 필터를 두면 저작 화면 결과와 실제 상담 동작이 갈라진다."""
    response = await client.post(
        "/internal/v1/rules/test",
        json={
            "tenant_id": TENANT,
            "text": "이 상품은 원금 보장됩니다. 900101-1234567 확인해주세요",
            "rules": [RULE],
        },
    )

    body = response.json()
    assert body["pii_masked"]
    assert "900101" not in body["clean_text"]
    assert body["matched_rules"] == ["FORBIDDEN_GUARANTEE"]


@pytest.mark.parametrize(
    ("pattern", "should_fail"),
    [
        ("원금.?보장", False),
        ("([unclosed", True),
        ("", True),
        (".*", True),  # 빈 문자열에 매칭 → 모든 발화에 걸린다
        ("a" * 600, True),
    ],
)
def test_pattern_validation(pattern: str, should_fail: bool) -> None:
    assert (validate_pattern(pattern) is not None) is should_fail


def test_export_drops_disabled_rules() -> None:
    """비활성 룰이 배포 산출물에 섞이면 끈 줄 알았던 룰이 계속 돈다."""
    from vai_contracts.authoring import ComplianceRuleSpec, RuleSet

    ruleset = RuleSet(
        tenant_id=TENANT,
        rules=[
            ComplianceRuleSpec(rule_id="ON", pattern="켜짐", enabled=True),
            ComplianceRuleSpec(rule_id="OFF", pattern="꺼짐", enabled=False),
        ],
    )

    exported = export_rules_for_pipeline(ruleset)

    assert "ON" in exported
    assert "OFF" not in exported


# ── STT 사전 ─────────────────────────────────────────────────────────────────


async def test_lexicon_round_trips_and_versions(client: httpx.AsyncClient) -> None:
    entry = {
        "surface": "무배당 행복플러스보험",
        "variants": ["무배당 행복 플러스 보험", "무배담 행복플러스"],
        "category": "product",
        "enabled": True,
    }

    saved = (
        await client.put(
            f"/internal/v1/lexicon/{TENANT}", json={"tenant_id": TENANT, "entries": [entry]}
        )
    ).json()
    again = (
        await client.put(
            f"/internal/v1/lexicon/{TENANT}", json={"tenant_id": TENANT, "entries": [entry]}
        )
    ).json()

    assert saved["version"] == 1
    assert again["version"] == 2, "저장할 때마다 버전이 올라 되돌릴 지점이 생겨야 한다"
    assert (await client.get(f"/internal/v1/lexicon/{TENANT}")).json()["entries"][0]["surface"] == (
        "무배당 행복플러스보험"
    )


async def test_empty_lexicon_is_returned_for_new_tenant(client: httpx.AsyncClient) -> None:
    body = (await client.get("/internal/v1/lexicon/brand-new")).json()

    assert body["entries"] == []


# ── 검색 튜닝 · 평가 ─────────────────────────────────────────────────────────


async def test_tuning_exposes_score_breakdown(client: httpx.AsyncClient) -> None:
    """어느 축에서 밀렸는지 모르면 운영자가 개선 방향을 못 정한다."""
    body = (
        await client.post(
            "/internal/v1/tuning/search",
            json={"tenant_id": TENANT, "kb_id": "default", "query": "결제일 연기"},
        )
    ).json()

    assert body["hits"]
    assert set(body) >= {"dense_ms", "sparse_ms", "rerank_ms"}
    assert set(body["hits"][0]) >= {"dense_score", "sparse_score", "rerank_score"}


async def test_eval_reports_recall_and_rank(client: httpx.AsyncClient) -> None:
    report = (
        await client.post(
            "/internal/v1/eval/run",
            json={
                "tenant_id": TENANT,
                "kb_id": "default",
                "cases": [
                    {
                        "case_id": "hit",
                        "query": "결제일 연기",
                        "expected_doc_ids": ["doc_card_policy_012"],
                    },
                    {"case_id": "miss", "query": "연회비", "expected_doc_ids": ["doc_nonexistent"]},
                ],
            },
        )
    ).json()

    assert report["total"] == 2
    assert report["hits"] == 1
    assert report["recall_at_k"] == 0.5
    by_id = {r["case_id"]: r for r in report["results"]}
    assert by_id["hit"]["rank"] == 1
    assert by_id["miss"]["rank"] is None


async def test_empty_eval_does_not_divide_by_zero(client: httpx.AsyncClient) -> None:
    report = (
        await client.post(
            "/internal/v1/eval/run", json={"tenant_id": TENANT, "kb_id": "default", "cases": []}
        )
    ).json()

    assert report["total"] == 0
    assert report["recall_at_k"] == 0.0


# ── 피드백 ───────────────────────────────────────────────────────────────────


async def test_feedback_surfaces_queries_to_improve(client: httpx.AsyncClient) -> None:
    """채택되지 않은 질의가 곧 다음 개선 작업의 우선순위다."""
    for accepted, query in [(True, "결제일"), (False, "환불 절차"), (False, "환불 절차")]:
        await client.post(
            "/internal/v1/feedback",
            json=PopupFeedback(
                session_id="s1", tenant_id=TENANT, doc_id="d1", query=query, accepted=accepted
            ).model_dump(mode="json"),
        )

    stats = (await client.get(f"/internal/v1/feedback/{TENANT}")).json()

    assert stats["total"] == 3
    assert stats["accepted"] == 1
    assert stats["worst_queries"][0] == "환불 절차"


async def test_feedback_stats_are_scoped_per_tenant(client: httpx.AsyncClient) -> None:
    await client.post(
        "/internal/v1/feedback",
        json=PopupFeedback(
            session_id="s1", tenant_id=TENANT, doc_id="d1", query="q", accepted=False
        ).model_dump(mode="json"),
    )

    assert (await client.get("/internal/v1/feedback/other")).json()["total"] == 0
