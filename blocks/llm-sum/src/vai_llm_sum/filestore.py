"""파일 기반 요약 저장소 — 재기동해도, 일주일이 지나도 사라지지 않는다.

Redis 저장소는 7일 TTL이었다. 상담 요약이라면 "이력 시스템으로 넘어갈 시간"으로
충분하지만, **회의록은 그 자체가 산출물이다.** 두 달 전 회의록을 찾는 것이
정상적인 사용이고, 그때 없으면 그건 제품 결함이다. PostgreSQL을 붙이는 대신
파일로 두는 이유는 폐쇄망이다 — DB 하나를 더 반입·운영·백업하게 만드는 비용이
얻는 것보다 크고, 요약은 세션당 한 건이라 파일 하나로 충분하다.

**감사 로그와 반대 방향으로 설계했다.** 감사 로그는 append-only 해시 체인이라
지울 수 없어야 증거가 되지만, 요약은 **지울 수 있어야 한다** — 개인정보 파기
요구가 오면 지워야 하고, 보관기간이 지나면 지워야 한다. 그래서 세션당 파일
하나로 두고 삭제가 파일 하나 삭제로 끝나게 했다.

레이아웃::

    <root>/<tenant_id>/<YYYY-MM-DD>/<session_id>.json

날짜를 경로에 넣는 이유는 두 가지다: 보관기간 만료 파기가 디렉토리 단위로
끝나고, 최신순 조회가 전체 스캔 없이 된다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vai_contracts.summary import Summary

log = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INDEX_NAME = ".index.json"
"""세션 ID → 날짜 디렉토리. 조회가 전체 디렉토리를 뒤지지 않게 한다."""


def _safe(value: str) -> str:
    """경로 조각으로 쓸 수 있게 다듬는다.

    ``session_id``와 ``tenant_id``는 바깥에서 들어온다. ``../``가 섞이면 저장소
    바깥에 파일을 쓰게 되므로 경로 구분자와 점을 통째로 막는다.
    """
    cleaned = _SAFE_NAME.sub("_", value).strip("._")
    return cleaned or "_"


class FileSummaryStore:
    """세션당 파일 하나. 재기동·TTL과 무관하게 남는다."""

    def __init__(self, root: Path, *, retention_days: int = 0) -> None:
        self._root = root
        self._retention_days = retention_days
        """0이면 자동 파기하지 않는다.

        기본값을 '무기한'으로 두는 것은 개인정보 관점에서 최선이 아니지만,
        **말없이 지우는 것보다는 낫다.** 보관기간은 고객사 정책이고, 이 값을
        정하지 않은 채 제품이 임의로 지우면 그건 데이터 소실 사고다.
        운영 콘솔과 기동 로그가 현재 정책을 드러낸다."""

        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def retention_days(self) -> int:
        return self._retention_days

    # ── 저장 ────────────────────────────────────────────────────────────────

    async def put(self, summary: Summary) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, summary)

    def _write(self, summary: Summary) -> None:
        tenant_dir = self._root / _safe(summary.tenant_id)
        day = summary.created_at.astimezone(UTC).strftime("%Y-%m-%d")

        previous = self._read_index(tenant_dir).get(summary.session_id)
        target_dir = tenant_dir / day
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{_safe(summary.session_id)}.json"

        # 부분 기록된 JSON이 남으면 이후 조회가 통째로 실패한다. 같은 파일시스템에
        # 임시 파일을 만들고 원자적으로 갈아 끼운다.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir, delete=False
        ) as handle:
            handle.write(summary.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)

        if previous and previous != day:
            # 수정으로 날짜가 바뀌면 옛 파일이 유령으로 남는다.
            stale = tenant_dir / previous / f"{_safe(summary.session_id)}.json"
            stale.unlink(missing_ok=True)
        self._update_index(tenant_dir, {summary.session_id: day})

    # ── 조회 ────────────────────────────────────────────────────────────────

    async def get(self, session_id: str) -> Summary | None:
        return await asyncio.to_thread(self._read, session_id)

    def _read(self, session_id: str) -> Summary | None:
        for tenant_dir in self._tenant_dirs():
            day = self._read_index(tenant_dir).get(session_id)
            if day is None:
                continue
            path = tenant_dir / day / f"{_safe(session_id)}.json"
            if path.is_file():
                return self._load(path)
        return None

    async def list_for(self, tenant_id: str, limit: int = 50) -> list[Summary]:
        return await asyncio.to_thread(self._list, tenant_id, limit)

    def _list(self, tenant_id: str, limit: int) -> list[Summary]:
        tenant_dir = self._root / _safe(tenant_id)
        if not tenant_dir.is_dir():
            return []

        found: list[Summary] = []
        # 최신 날짜부터 훑고 limit을 채우면 멈춘다 — 3년치가 쌓여도 조회는
        # 최근 며칠만 읽는다.
        for day in sorted(self._day_dirs(tenant_dir), reverse=True):
            for path in sorted(day.glob("*.json"), reverse=True):
                summary = self._load(path)
                if summary is not None:
                    found.append(summary)
            if len(found) >= limit:
                break
        found.sort(key=lambda item: item.created_at, reverse=True)
        return found[:limit]

    def _load(self, path: Path) -> Summary | None:
        try:
            return Summary.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 한 건이 깨졌다고 목록 전체를 실패시키지 않는다. 다만 조용히
            # 넘기지도 않는다 — 이 로그가 복구 대상 목록이 된다.
            log.warning("요약 파일을 읽을 수 없다", extra={"path": str(path)})
            return None

    # ── 파기 ────────────────────────────────────────────────────────────────

    async def delete(self, session_id: str) -> bool:
        """요약 1건 파기. 개인정보 파기 요구에 응할 수 있어야 한다(docs/05 §2.2)."""
        async with self._lock:
            return await asyncio.to_thread(self._delete, session_id)

    def _delete(self, session_id: str) -> bool:
        removed = False
        for tenant_dir in self._tenant_dirs():
            index = self._read_index(tenant_dir)
            day = index.pop(session_id, None)
            if day is None:
                continue
            path = tenant_dir / day / f"{_safe(session_id)}.json"
            if path.is_file():
                path.unlink()
                removed = True
            self._write_index(tenant_dir, index)
        return removed

    async def purge_expired(self) -> int:
        """보관기간이 지난 날짜 디렉토리를 통째로 지운다. 지운 건수를 돌려준다."""
        if self._retention_days <= 0:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._purge)

    def _purge(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=self._retention_days)).strftime("%Y-%m-%d")
        removed = 0
        for tenant_dir in self._tenant_dirs():
            index = self._read_index(tenant_dir)
            doomed = [day for day in self._day_dirs(tenant_dir) if day.name < cutoff]
            for day in doomed:
                removed += len(list(day.glob("*.json")))
                shutil.rmtree(day, ignore_errors=True)
            if doomed:
                gone = {day.name for day in doomed}
                self._write_index(tenant_dir, {sid: d for sid, d in index.items() if d not in gone})
        if removed:
            log.info(
                "보관기간 만료 요약 파기",
                extra={"removed": removed, "retention_days": self._retention_days},
            )
        return removed

    # ── 내부 ────────────────────────────────────────────────────────────────

    def _tenant_dirs(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        return [path for path in sorted(self._root.iterdir()) if path.is_dir()]

    def _day_dirs(self, tenant_dir: Path) -> list[Path]:
        return [
            path for path in tenant_dir.iterdir() if path.is_dir() and _DATE_DIR.match(path.name)
        ]

    def _index_path(self, tenant_dir: Path) -> Path:
        return tenant_dir / INDEX_NAME

    def _read_index(self, tenant_dir: Path) -> dict[str, str]:
        path = self._index_path(tenant_dir)
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 인덱스는 파생 데이터다. 깨지면 다시 만들면 되므로 조회를 막지 않는다.
            log.warning("요약 인덱스 손상 — 재구성", extra={"path": str(path)})
            return self._rebuild_index(tenant_dir)
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _rebuild_index(self, tenant_dir: Path) -> dict[str, str]:
        index = {
            path.stem: day.name for day in self._day_dirs(tenant_dir) for path in day.glob("*.json")
        }
        self._write_index(tenant_dir, index)
        return index

    def _write_index(self, tenant_dir: Path, index: dict[str, str]) -> None:
        tenant_dir.mkdir(parents=True, exist_ok=True)
        path = self._index_path(tenant_dir)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=tenant_dir, delete=False
        ) as handle:
            json.dump(index, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)

    def _update_index(self, tenant_dir: Path, entries: dict[str, str]) -> None:
        index = self._read_index(tenant_dir)
        index.update(entries)
        self._write_index(tenant_dir, index)
