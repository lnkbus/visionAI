"""해시 체인 감사 저장소.

감사 로그의 가치는 "변조되지 않았음을 증명할 수 있는가"에서 나온다. 지우거나
고칠 수 있는 로그는 감사에서 증거로 쓰이지 못한다.

각 레코드는 앞 레코드의 해시를 품는다. 한 줄을 고치거나 지우면 그 뒤 모든
해시가 어긋나고, 검증은 **몇 번째 레코드에서** 끊겼는지까지 짚어 준다.

**날짜별 세그먼트로 나누되 체인은 끊지 않는다.** 단일 파일은 몇 달이면
수 GB가 되어 검증 한 번에 전부 읽어야 하고, 백업·반출 단위로도 다루기 어렵다.
새 세그먼트의 첫 레코드는 앞 세그먼트의 마지막 해시를 이어받는다.

**이것은 변조를 막지 않고 탐지한다.** root 권한자는 파일 전체를 다시 쓸 수
있다. 완전한 방어는 head 해시를 외부에 정기 공증(일일 출력·별도 매체 보관)하는
운영 절차와 함께여야 성립한다 — 그 사실을 문서에 적어 둔다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from vai_contracts.audit import AuditAction, AuditEvent, AuditRecord, ChainStatus

log = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64
"""최초 레코드의 ``prev_hash``. 체인의 시작점을 고정한다."""

SEGMENT_PREFIX = "audit-"
SEGMENT_SUFFIX = ".jsonl"


def _digest(record: AuditRecord) -> str:
    """레코드 해시. ``entry_hash``를 뺀 나머지 전체를 대상으로 한다.

    정규화(sort_keys·공백 제거)를 고정해야 같은 레코드가 항상 같은 해시를
    낸다. 여기가 흔들리면 정상 로그가 변조로 보고된다.
    """
    body = {
        "seq_no": record.seq_no,
        "prev_hash": record.prev_hash,
        "recorded_at": record.recorded_at.isoformat(),
        "event": record.event.model_dump(mode="json"),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def segment_name(day: date) -> str:
    return f"{SEGMENT_PREFIX}{day:%Y%m%d}{SEGMENT_SUFFIX}"


class AuditChain:
    """append-only 감사 저장소.

    한 프로세스 안에서 워커(버스 소비)와 API(동기 기록)가 함께 쓰므로
    쓰기는 락으로 직렬화한다. 여러 프로세스가 같은 디렉토리에 쓰면 체인이
    깨지므로 CORE-SEC은 단일 인스턴스로 배치한다.
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._lock = asyncio.Lock()
        self._head_hash = GENESIS_HASH
        self._seq_no = 0
        self._loaded = False

    @property
    def head_hash(self) -> str:
        return self._head_hash

    @property
    def count(self) -> int:
        return self._seq_no

    def load(self) -> None:
        """기존 세그먼트를 훑어 체인의 끝을 찾는다.

        재기동 때마다 전체를 읽지만, 마지막 상태만 알면 되므로 파싱은
        가볍게 유지한다. 검증은 별도 API에서 한다.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        # 감사 로그 디렉토리는 소유자만 본다. 열려 있으면 그 자체가 유출 경로다.
        os.chmod(self._dir, 0o700)

        last: AuditRecord | None = None
        for record in self._iter_records():
            last = record
        if last is not None:
            self._head_hash = last.entry_hash
            self._seq_no = last.seq_no
        self._loaded = True
        log.info("감사 체인 적재", extra={"records": self._seq_no, "head": self._head_hash[:16]})

    async def append(self, event: AuditEvent) -> AuditRecord:
        """레코드를 추가한다. 쓰기가 끝난 뒤에야 반환한다.

        fsync까지 기다리는 이유: PII 열람처럼 "기록 성공 후 허용"이 필요한
        경로가 이 반환값을 신뢰한다. 버퍼에만 있고 디스크에 없으면
        전원이 나갔을 때 열람 사실이 사라진다.
        """
        if not self._loaded:
            self.load()

        async with self._lock:
            record = AuditRecord(
                seq_no=self._seq_no + 1,
                prev_hash=self._head_hash,
                entry_hash="",
                recorded_at=datetime.now(UTC),
                event=event,
            )
            record.entry_hash = _digest(record)
            await asyncio.to_thread(self._write, record)
            self._head_hash = record.entry_hash
            self._seq_no = record.seq_no
            return record

    def _write(self, record: AuditRecord) -> None:
        path = self._dir / segment_name(record.recorded_at.date())
        line = record.model_dump_json() + "\n"
        # O_APPEND는 같은 프로세스의 동시 쓰기에서도 줄이 섞이지 않게 한다.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def segments(self) -> list[Path]:
        """세그먼트를 시간 순으로. 이름이 곧 날짜라 사전순 정렬이면 충분하다."""
        return sorted(self._dir.glob(f"{SEGMENT_PREFIX}*{SEGMENT_SUFFIX}"))

    def _iter_records(self) -> Iterator[AuditRecord]:
        for segment in self.segments():
            with segment.open(encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield AuditRecord.model_validate_json(line)
                    except ValueError:
                        # 깨진 줄에서 멈추면 뒤쪽 정상 기록까지 못 본다.
                        # 검증이 이 사실을 별도로 잡아낸다.
                        log.error(
                            "감사 레코드를 해석할 수 없다",
                            extra={"segment": segment.name, "line": lineno},
                        )

    def verify(self) -> ChainStatus:
        """전체 체인을 재계산해 무결성을 확인한다.

        끊긴 지점을 ``broken_at``으로 알려 준다 — 감사에서는 "깨졌다"보다
        "어디서부터 믿을 수 없는가"가 실제로 필요한 정보다.
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        total = 0
        head = GENESIS_HASH

        for record in self._iter_records():
            total += 1
            if record.seq_no != expected_seq:
                return ChainStatus(
                    total=total,
                    intact=False,
                    broken_at=record.seq_no,
                    reason=f"일련번호 불연속: {expected_seq} 자리에 {record.seq_no} — 삭제 흔적",
                    head_hash=head,
                )
            if record.prev_hash != expected_prev:
                return ChainStatus(
                    total=total,
                    intact=False,
                    broken_at=record.seq_no,
                    reason="앞 레코드 해시가 어긋난다 — 이 지점 앞이 변조됐다",
                    head_hash=head,
                )
            if _digest(record) != record.entry_hash:
                return ChainStatus(
                    total=total,
                    intact=False,
                    broken_at=record.seq_no,
                    reason="레코드 내용이 자기 해시와 맞지 않는다 — 이 레코드가 변조됐다",
                    head_hash=head,
                )
            expected_prev = head = record.entry_hash
            expected_seq += 1

        return ChainStatus(total=total, intact=True, head_hash=head)

    def query(
        self,
        *,
        actor: str = "",
        action: AuditAction | None = None,
        tenant_id: str = "",
        session_id: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[AuditRecord]:
        """감사 조회. 최신 것부터 ``limit``개.

        세그먼트를 **뒤에서부터** 읽는다. 감사 조회는 대개 최근 사건을 보므로
        수개월치를 앞에서부터 훑는 것은 낭비다.
        """
        found: list[AuditRecord] = []
        for segment in reversed(self.segments()):
            lines = segment.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    record = AuditRecord.model_validate_json(line)
                except ValueError:
                    continue
                if _matches(record, actor, action, tenant_id, session_id, since, until):
                    found.append(record)
                    if len(found) >= limit:
                        return found
        return found


def _matches(
    record: AuditRecord,
    actor: str,
    action: AuditAction | None,
    tenant_id: str,
    session_id: str,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    event = record.event
    if actor and event.actor != actor:
        return False
    if action is not None and event.action != action:
        return False
    if tenant_id and event.tenant_id != tenant_id:
        return False
    if session_id and event.session_id != session_id:
        return False
    if since is not None and record.recorded_at < since:
        return False
    return not (until is not None and record.recorded_at > until)
