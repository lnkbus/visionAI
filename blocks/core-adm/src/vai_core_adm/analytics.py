"""운영 통계 수집과 콜 이력.

CORE-ADM은 원래 **집계만** 한다 — 블록 상태의 원본은 각 블록이 갖고 있으므로
따로 저장하면 원본과 갈라진다. 통계는 사정이 다르다. **지나간 이벤트는 아무도
갖고 있지 않다.** 여기서 세지 않으면 "어제 인식률이 어땠나"에 답할 원본 자체가
존재하지 않는다. 그래서 이 모듈만 예외적으로 쓴다.

담는 것은 **마스킹된 텍스트뿐**이다(``filter.clean`` 산출물). 이력 조회 화면은
개인정보 열람 창구가 되기 가장 쉬운 자리이고, 한 번 새면 회수할 수 없다.

수집은 **실시간 경로 밖**에서 돈다. 통계 저장이 느려도 상담은 그대로 돌아야
하므로, 여기는 별도 컨슈머 그룹으로 스트림을 읽을 뿐 파이프라인을 잡지 않는다.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.analytics import (
    CallRecord,
    CallTurn,
    DailyStats,
    FallbackUtterance,
    RankEntry,
    StatsOverview,
)
from vai_contracts.dialog import BotTurn
from vai_contracts.events import FilterResult, SessionClosed, SttDelta
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.summary import SummaryDone, SummaryStatus
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)
BLOCK_ID = "CORE-ADM"

NS = "vai:stats"
MAX_TURNS = 500
"""이력에 남길 발화 수 상한.

한 통화가 수천 발화가 되는 일은 없지만, 끊기지 않은 세션 하나가 저장소를
가득 채우는 일은 있다. 상한을 넘으면 잘라 두고 **잘랐다는 사실을 기록에
남긴다** — 숨기면 이력을 증거로 쓸 수 없다."""

MAX_FALLBACKS = 500
DEFAULT_RETENTION_DAYS = 90

_FLOAT_FIELDS = {"confidence_sum"}
"""나머지는 정수다. 평균이 아니라 합계와 개수를 따로 담기 때문에, 기간을
합칠 때 '평균의 평균'이 되지 않는다."""

_WS = re.compile(r"\s+")

_ROLE_LABEL = {ChannelRole.CUSTOMER: "고객", ChannelRole.AGENT: "상담원"}


def day_key(when: datetime | None = None) -> str:
    return (when or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")


def recent_days(days: int, *, now: datetime | None = None) -> list[str]:
    """오늘부터 거슬러 ``days``일. 오래된 날이 앞에 온다."""
    base = (now or datetime.now(UTC)).astimezone(UTC)
    return [
        (base - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(max(1, days) - 1, -1, -1)
    ]


def normalize_utterance(text: str) -> str:
    """폴백 발화 묶음 키. 공백과 대소문자만 흡수한다.

    더 세게 정규화하면(조사 제거 등) 서로 다른 질문이 한 칸에 뭉쳐 재학습
    자료로서의 가치가 사라진다.
    """
    return _WS.sub(" ", text).strip().lower()


class AnalyticsStore(ABC):
    """통계·이력 저장소."""

    @abstractmethod
    async def bump(self, tenant_id: str, day: str, fields: dict[str, float]) -> None: ...

    @abstractmethod
    async def daily(self, tenant_id: str, days: list[str]) -> list[DailyStats]: ...

    @abstractmethod
    async def rank(self, tenant_id: str, kind: str, key: str, label: str = "") -> None: ...

    @abstractmethod
    async def rankings(self, tenant_id: str, kind: str, limit: int = 20) -> list[RankEntry]: ...

    @abstractmethod
    async def add_fallback(self, tenant_id: str, utterance: FallbackUtterance) -> None: ...

    @abstractmethod
    async def fallbacks(self, tenant_id: str, limit: int = 50) -> list[FallbackUtterance]: ...

    @abstractmethod
    async def put_call(self, record: CallRecord) -> None: ...

    @abstractmethod
    async def get_call(self, session_id: str) -> CallRecord | None: ...

    @abstractmethod
    async def list_calls(
        self, tenant_id: str, *, limit: int = 50, query: str = ""
    ) -> list[CallRecord]: ...

    @abstractmethod
    async def delete_call(self, session_id: str) -> bool: ...


class InMemoryAnalyticsStore(AnalyticsStore):
    """단위 시험·단일 프로세스용. 재기동하면 사라진다."""

    def __init__(self) -> None:
        self._daily: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        self._ranks: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
        self._labels: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        self._fallbacks: dict[str, dict[str, FallbackUtterance]] = defaultdict(dict)
        self._calls: dict[str, CallRecord] = {}

    async def bump(self, tenant_id: str, day: str, fields: dict[str, float]) -> None:
        bucket = self._daily[(tenant_id, day)]
        for name, amount in fields.items():
            bucket[name] = bucket.get(name, 0.0) + amount

    async def daily(self, tenant_id: str, days: list[str]) -> list[DailyStats]:
        return [_to_stats(day, self._daily.get((tenant_id, day), {})) for day in days]

    async def rank(self, tenant_id: str, kind: str, key: str, label: str = "") -> None:
        counts = self._ranks[(tenant_id, kind)]
        counts[key] = counts.get(key, 0) + 1
        if label:
            self._labels[(tenant_id, kind)][key] = label

    async def rankings(self, tenant_id: str, kind: str, limit: int = 20) -> list[RankEntry]:
        counts = self._ranks.get((tenant_id, kind), {})
        labels = self._labels.get((tenant_id, kind), {})
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [RankEntry(key=k, label=labels.get(k, ""), count=c) for k, c in top]

    async def add_fallback(self, tenant_id: str, utterance: FallbackUtterance) -> None:
        bucket = self._fallbacks[tenant_id]
        key = normalize_utterance(utterance.text)
        existing = bucket.get(key)
        if existing is not None:
            existing.count += 1
            existing.last_seen = utterance.last_seen
            existing.session_id = utterance.session_id
        else:
            if len(bucket) >= MAX_FALLBACKS:
                # 가장 오래된 것을 버린다. 새 발화가 못 들어오면 재학습
                # 목록이 과거에 고정되고, 그건 목록이 없는 것과 같다.
                oldest = min(bucket, key=lambda k: bucket[k].last_seen)
                bucket.pop(oldest, None)
            bucket[key] = utterance

    async def fallbacks(self, tenant_id: str, limit: int = 50) -> list[FallbackUtterance]:
        items = list(self._fallbacks.get(tenant_id, {}).values())
        items.sort(key=lambda u: (-u.count, u.text))
        return items[:limit]

    async def put_call(self, record: CallRecord) -> None:
        self._calls[record.session_id] = record

    async def get_call(self, session_id: str) -> CallRecord | None:
        return self._calls.get(session_id)

    async def list_calls(
        self, tenant_id: str, *, limit: int = 50, query: str = ""
    ) -> list[CallRecord]:
        items = [c for c in self._calls.values() if c.tenant_id == tenant_id]
        if query:
            items = [c for c in items if _matches(c, query)]
        items.sort(key=lambda c: c.ended_at or c.started_at, reverse=True)
        return items[:limit]

    async def delete_call(self, session_id: str) -> bool:
        return self._calls.pop(session_id, None) is not None


class RedisAnalyticsStore(AnalyticsStore):
    """운영 기본값. 모든 키에 보관기간 TTL이 붙는다.

    TTL을 거는 이유는 용량이 아니라 **개인정보다**. 보관기간을 정하지 않은
    대화 이력은 시간이 갈수록 위험만 커지고 쓸모는 줄어든다.
    """

    def __init__(self, redis: Redis, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self._redis = redis
        self._ttl = max(1, retention_days) * 86400

    def _k(self, *parts: str) -> str:
        return ":".join((NS, *parts))

    async def bump(self, tenant_id: str, day: str, fields: dict[str, float]) -> None:
        key = self._k("daily", tenant_id, day)
        pipe = self._redis.pipeline()
        for name, amount in fields.items():
            if name in _FLOAT_FIELDS:
                pipe.hincrbyfloat(key, name, amount)
            else:
                pipe.hincrby(key, name, int(amount))
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def daily(self, tenant_id: str, days: list[str]) -> list[DailyStats]:
        pipe = self._redis.pipeline()
        for day in days:
            pipe.hgetall(self._k("daily", tenant_id, day))
        rows = await pipe.execute()
        return [_to_stats(day, _decode(row)) for day, row in zip(days, rows, strict=True)]

    async def rank(self, tenant_id: str, kind: str, key: str, label: str = "") -> None:
        zkey = self._k("rank", kind, tenant_id)
        pipe = self._redis.pipeline()
        pipe.zincrby(zkey, 1, key)
        pipe.expire(zkey, self._ttl)
        if label:
            lkey = self._k("ranklabel", kind, tenant_id)
            pipe.hset(lkey, key, label)
            pipe.expire(lkey, self._ttl)
        await pipe.execute()

    async def rankings(self, tenant_id: str, kind: str, limit: int = 20) -> list[RankEntry]:
        rows = await self._redis.zrevrange(
            self._k("rank", kind, tenant_id), 0, max(0, limit - 1), withscores=True
        )
        if not rows:
            return []
        keys = [_text(row[0]) for row in rows]
        labels = await self._redis.hmget(self._k("ranklabel", kind, tenant_id), keys)
        entries = []
        for index, key in enumerate(keys):
            label = labels[index] if index < len(labels) else None
            entries.append(
                RankEntry(
                    key=key,
                    label=_text(label) if label else "",
                    count=int(float(rows[index][1])),
                )
            )
        return entries

    async def add_fallback(self, tenant_id: str, utterance: FallbackUtterance) -> None:
        key = self._k("fallback", tenant_id)
        field = normalize_utterance(utterance.text)
        raw = await self._redis.hget(key, field)
        if raw:
            existing = FallbackUtterance.model_validate_json(_text(raw))
            utterance.count = existing.count + 1
        pipe = self._redis.pipeline()
        pipe.hset(key, field, utterance.model_dump_json())
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def fallbacks(self, tenant_id: str, limit: int = 50) -> list[FallbackUtterance]:
        raw = await self._redis.hgetall(self._k("fallback", tenant_id))
        items = [FallbackUtterance.model_validate_json(_text(v)) for v in raw.values()]
        items.sort(key=lambda u: (-u.count, u.text))
        return items[:limit]

    async def put_call(self, record: CallRecord) -> None:
        stamp = (record.ended_at or record.started_at).timestamp()
        pipe = self._redis.pipeline()
        pipe.set(self._k("call", record.session_id), record.model_dump_json(), ex=self._ttl)
        index = self._k("calls", record.tenant_id)
        pipe.zadd(index, {record.session_id: stamp})
        pipe.expire(index, self._ttl)
        await pipe.execute()

    async def get_call(self, session_id: str) -> CallRecord | None:
        raw = await self._redis.get(self._k("call", session_id))
        return CallRecord.model_validate_json(_text(raw)) if raw else None

    async def list_calls(
        self, tenant_id: str, *, limit: int = 50, query: str = ""
    ) -> list[CallRecord]:
        # 검색이 있으면 더 넓게 훑는다. 최신 50건만 보고 "없다"고 답하면
        # 화면은 조용히 틀린 말을 하게 된다.
        window = limit if not query else min(1000, limit * 20)
        ids = await self._redis.zrevrange(self._k("calls", tenant_id), 0, max(0, window - 1))
        if not ids:
            return []
        rows = await self._redis.mget([self._k("call", _text(i)) for i in ids])
        records = [CallRecord.model_validate_json(_text(r)) for r in rows if r]
        if query:
            records = [r for r in records if _matches(r, query)]
        return records[:limit]

    async def delete_call(self, session_id: str) -> bool:
        record = await self.get_call(session_id)
        if record is None:
            return False
        pipe = self._redis.pipeline()
        pipe.delete(self._k("call", session_id))
        pipe.zrem(self._k("calls", record.tenant_id), session_id)
        await pipe.execute()
        return True


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _decode(row: dict[object, object]) -> dict[str, str]:
    return {_text(k): _text(v) for k, v in row.items()}


def _to_stats(day: str, row: Mapping[str, object]) -> DailyStats:
    """저장된 칸을 집계 모델로 옮긴다. **모르는 칸은 버린다.**

    통계 필드는 시간이 지나며 늘어난다. 예전 버전이 쓴 칸이 남아 있다고 조회가
    실패하면 화면이 통째로 비고, 그건 가장 나쁜 결과다.
    """
    known = set(DailyStats.model_fields) - {"day"}
    payload: dict[str, object] = {k: v for k, v in row.items() if k in known}
    payload["day"] = day
    return DailyStats.model_validate(payload)


def _matches(record: CallRecord, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    if needle in record.session_id.lower():
        return True
    return any(needle in turn.text.lower() for turn in record.turns)


class _Accumulator:
    """진행 중인 세션의 이력을 모은다.

    세션이 끝나야 한 건이 완성되므로 중간 상태를 들고 있어야 하고, 그러면
    **버려야 할 때 버리는 것**이 중요해진다. 24시간 도는 온프렘에서 이 딕셔너리가
    새면 어느 날 조용히 죽는다 — :class:`SessionReaper` 가 해제한다.
    """

    def __init__(self) -> None:
        self.records: dict[str, CallRecord] = {}
        self.last_customer: dict[str, str] = {}

    def get(self, session_id: str, tenant_id: str) -> CallRecord:
        record = self.records.get(session_id)
        if record is None:
            record = CallRecord(session_id=session_id, tenant_id=tenant_id)
            self.records[session_id] = record
        return record

    def drop(self, session_id: str) -> CallRecord | None:
        self.last_customer.pop(session_id, None)
        return self.records.pop(session_id, None)


class UtteranceCollector(BlockWorker[FilterResult]):
    """``filter.clean`` — 발화 수·마스킹·룰 적중, 그리고 이력 본문."""

    block_id = BLOCK_ID
    source_topic = Topic.FILTER_CLEAN
    source_model = FilterResult

    def __init__(
        self, bus: EventBus, store: AnalyticsStore, acc: _Accumulator, *, group: str, consumer: str
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._store = store
        self._acc = acc

    async def handle(self, event: FilterResult) -> None:
        if not event.is_final or not event.clean_text.strip():
            return

        fields: dict[str, float] = {"utterances": 1}
        if event.pii_masked:
            fields["pii_masked"] = 1
        if event.matched_rules:
            fields["rule_hits"] = len(event.matched_rules)
        await self._store.bump(event.tenant_id, day_key(), fields)

        for rule in event.matched_rules:
            await self._store.rank(event.tenant_id, "rule", rule.rule_id, rule.message)

        record = self._acc.get(event.session_id, event.tenant_id)
        record.utterances += 1
        if event.pii_masked:
            record.pii_masked += 1
        for rule in event.matched_rules:
            if rule.rule_id not in record.rule_hits:
                record.rule_hits.append(rule.rule_id)
        if len(record.turns) < MAX_TURNS:
            record.turns.append(
                CallTurn(
                    at_ms=event.start_ms,
                    speaker=event.speaker_id or _ROLE_LABEL.get(event.channel, "참여자"),
                    text=event.clean_text.strip(),
                )
            )
        else:
            record.truncated = True

        if event.channel is ChannelRole.CUSTOMER:
            # 폴백 후보. 봇이 손을 들면 **직전에 고객이 한 말**이 재학습 자료다.
            self._acc.last_customer[event.session_id] = event.clean_text.strip()

    def release_session(self, session_id: str) -> None:
        self._acc.last_customer.pop(session_id, None)


class RecognitionCollector(BlockWorker[SttDelta]):
    """``stt.delta`` — 인식 신뢰도와 음성 길이.

    ``filter.clean`` 에는 신뢰도가 없다. 마스킹은 텍스트를 다루지 인식 품질을
    다루지 않기 때문이며, 그래서 이 지표만 원본 토픽에서 따로 센다.
    """

    block_id = BLOCK_ID
    source_topic = Topic.STT_DELTA
    source_model = SttDelta

    def __init__(self, bus: EventBus, store: AnalyticsStore, *, group: str, consumer: str) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._store = store

    async def handle(self, event: SttDelta) -> None:
        if not event.is_final:
            # 중간 결과를 세면 같은 말이 여러 번 잡혀 평균이 흐려진다.
            return
        await self._store.bump(
            event.tenant_id,
            day_key(),
            {
                "confidence_sum": event.confidence,
                "confidence_n": 1,
                "audio_ms": event.duration_ms,
            },
        )


class BotTurnCollector(BlockWorker[BotTurn]):
    """``bot.turn`` — 플로우 랭킹과 폴백 발화."""

    block_id = BLOCK_ID
    source_topic = Topic.BOT_TURN
    source_model = BotTurn

    def __init__(
        self, bus: EventBus, store: AnalyticsStore, acc: _Accumulator, *, group: str, consumer: str
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._store = store
        self._acc = acc

    async def handle(self, event: BotTurn) -> None:
        fields: dict[str, float] = {"bot_turns": 1}
        if event.handed_off:
            fields["handoffs"] = 1
        await self._store.bump(event.tenant_id, day_key(), fields)
        await self._store.rank(event.tenant_id, "node", event.node_id)

        record = self._acc.get(event.session_id, event.tenant_id)
        record.bot_turns += 1
        if len(record.turns) < MAX_TURNS:
            record.turns.append(CallTurn(speaker="봇", text=event.text, is_bot=True))

        if not event.handed_off:
            return
        record.handed_off = True
        spoken = self._acc.last_customer.get(event.session_id, "")
        if spoken:
            # 사람에게 넘어간 순간의 직전 발화. 무엇을 물었는데 못 알아들었는지가
            # 인텐트 보강의 유일한 1차 자료다.
            await self._store.add_fallback(
                event.tenant_id,
                FallbackUtterance(text=spoken, node_id=event.node_id, session_id=event.session_id),
            )


class SummaryStatsCollector(BlockWorker[SummaryDone]):
    """``summary.done`` — 요약 성공률·지연·양보 대기."""

    block_id = BLOCK_ID
    source_topic = Topic.SUMMARY_DONE
    source_model = SummaryDone

    def __init__(
        self, bus: EventBus, store: AnalyticsStore, acc: _Accumulator, *, group: str, consumer: str
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._store = store
        self._acc = acc

    async def handle(self, event: SummaryDone) -> None:
        ready = event.status is SummaryStatus.READY
        fields: dict[str, float] = {
            "summaries_ready" if ready else "summaries_failed": 1,
            "summary_waited_sum": event.waited_for_stt_ms,
        }
        if ready and event.latency_ms:
            fields["summary_latency_sum"] = event.latency_ms
            fields["summary_latency_n"] = 1
        if event.yield_gave_up:
            fields["summary_gave_up"] = 1
        await self._store.bump(event.tenant_id, day_key(), fields)

        # 요약은 세션 종료 **뒤에** 끝난다. 이력은 이미 저장돼 있으므로
        # 결과만 덧붙인다 — 여기서 새로 만들면 발화 없는 빈 건이 하나 더 생긴다.
        record = await self._store.get_call(event.session_id)
        if record is not None:
            record.summary_status = event.status.value
            await self._store.put_call(record)


class SessionCollector(BlockWorker[SessionClosed]):
    """``session.closed`` — 세션 수를 세고 이력 한 건을 확정한다."""

    block_id = BLOCK_ID
    source_topic = Topic.SESSION_CLOSED
    source_model = SessionClosed

    def __init__(
        self, bus: EventBus, store: AnalyticsStore, acc: _Accumulator, *, group: str, consumer: str
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._store = store
        self._acc = acc

    async def handle(self, event: SessionClosed) -> None:
        meeting = event.profile is SessionProfile.MEETING
        await self._store.bump(
            event.tenant_id,
            day_key(),
            {
                "sessions": 1,
                "sessions_meeting" if meeting else "sessions_aicc": 1,
            },
        )

        record = self._acc.drop(event.session_id)
        if record is None:
            # 발화가 하나도 없던 세션(오접속·즉시 끊김). 이력에 빈 건을 남기면
            # 조회 화면이 쓰레기로 찬다.
            return
        record.profile = event.profile
        record.ended_at = datetime.now(UTC)
        record.duration_ms = event.duration_ms
        await self._store.put_call(record)


def overview(tenant_id: str, rows: list[DailyStats]) -> StatsOverview:
    """일별 집계를 기간 합계로 접는다.

    합계와 개수를 각각 더한 뒤 나눈다. 일별 평균을 다시 평균하면 통화 수가
    다른 날들 사이에서 틀린 값이 나온다.
    """
    total = StatsOverview(
        tenant_id=tenant_id,
        days=len(rows),
        since=rows[0].day if rows else "",
        until=rows[-1].day if rows else "",
        daily=rows,
    )
    conf_sum = conf_n = 0.0
    lat_sum = lat_n = 0
    for row in rows:
        total.sessions += row.sessions
        total.utterances += row.utterances
        total.pii_masked += row.pii_masked
        total.rule_hits += row.rule_hits
        total.handoffs += row.handoffs
        total.summaries_ready += row.summaries_ready
        total.summaries_failed += row.summaries_failed
        total.summary_gave_up += row.summary_gave_up
        conf_sum += row.confidence_sum
        conf_n += row.confidence_n
        lat_sum += row.summary_latency_sum
        lat_n += row.summary_latency_n

    bot_turns = sum(row.bot_turns for row in rows)
    total.mean_confidence = conf_sum / conf_n if conf_n else 0.0
    total.mean_summary_latency_ms = lat_sum / lat_n if lat_n else 0.0
    total.handoff_rate = total.handoffs / bot_turns if bot_turns else 0.0
    finished = total.summaries_ready + total.summaries_failed
    total.summary_success_rate = total.summaries_ready / finished if finished else 0.0
    return total


def build_store(bus: EventBus, *, retention_days: int) -> AnalyticsStore:
    """Redis 버스면 Redis 저장소. 아니면 인메모리 — 재기동하면 사라진다."""
    from vai_common.bus import RedisEventBus

    if isinstance(bus, RedisEventBus):
        return RedisAnalyticsStore(bus.redis, retention_days=retention_days)
    log.warning("Redis 버스가 아니다 — 통계를 메모리에만 담는다(재기동 시 소실)")
    return InMemoryAnalyticsStore()


def build_collectors(
    bus: EventBus, store: AnalyticsStore, *, group: str, consumer: str
) -> tuple[list[BlockWorker[Any]], _Accumulator]:
    """수집 워커 한 벌.

    **토픽마다 컨슈머 그룹을 따로 쓴다.** 같은 그룹을 나눠 쓰면 다른 블록과
    메시지를 나눠 갖게 되고, 통계가 조용히 절반만 잡힌다.
    """
    acc = _Accumulator()
    workers: list[BlockWorker[Any]] = [
        UtteranceCollector(bus, store, acc, group=f"{group}:utterance", consumer=consumer),
        RecognitionCollector(bus, store, group=f"{group}:stt", consumer=consumer),
        BotTurnCollector(bus, store, acc, group=f"{group}:bot", consumer=consumer),
        SummaryStatsCollector(bus, store, acc, group=f"{group}:summary", consumer=consumer),
        SessionCollector(bus, store, acc, group=f"{group}:session", consumer=consumer),
    ]
    return workers, acc


__all__ = [
    "AnalyticsStore",
    "InMemoryAnalyticsStore",
    "RedisAnalyticsStore",
    "build_collectors",
    "build_store",
    "day_key",
    "normalize_utterance",
    "overview",
    "recent_days",
]
