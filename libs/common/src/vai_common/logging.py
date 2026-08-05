"""구조화 로깅.

폐쇄망 고객사는 자사 로그 수집 체계를 쓰므로 JSON 라인 출력을 켤 수 있어야 하고,
개발 중에는 사람이 읽는 형식이 편하다. 두 포맷을 설정으로 전환한다.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, block_id: str) -> None:
        super().__init__()
        self.block_id = block_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "block": self.block_id,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # logger.info("...", extra={"session_id": ...}) 로 붙인 필드를 그대로 승격
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(block_id: str, level: str = "INFO", *, as_json: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if as_json:
        handler.setFormatter(JsonFormatter(block_id))
    else:
        handler.setFormatter(
            logging.Formatter(f"%(asctime)s %(levelname)-5s [{block_id}] %(name)s — %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
