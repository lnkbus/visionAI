"""구조화 로깅.

폐쇄망 고객사는 자사 로그 수집 체계를 쓰므로 JSON 라인 출력을 켤 수 있어야 하고,
개발 중에는 사람이 읽는 형식이 편하다. 두 포맷을 설정으로 전환한다.

**두 포맷은 같은 것을 담는다.** `extra={...}` 로 붙인 필드는 JSON 에서만
승격되고 사람이 읽는 형식에서는 버려졌었다. 그래서 이런 줄이 나왔다:

    INFO [STT-CORE] ...faster_whisper — STT 장치 자동 선택

무엇으로 선택했는지가 없다. GPU 냐 CPU 냐를 알아보려고 켠 로그인데 정작
그 답이 지워진 것이고, 없어진 자리가 하필 **사람이 읽는 쪽**이었다.
"""

from __future__ import annotations

import copy
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


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class TextFormatter(logging.Formatter):
    """사람이 읽는 형식. `extra=` 필드를 메시지 뒤에 이어 붙인다.

    붙이는 순서는 부른 쪽이 적은 순서 그대로다 — 중요한 것을 먼저 적었을
    테니 그 뜻을 지킨다.
    """

    def __init__(self, block_id: str) -> None:
        super().__init__(f"%(asctime)s %(levelname)-5s [{block_id}] %(name)s — %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        extras = _extras(record)
        if not extras:
            return super().format(record)

        # 레코드를 복사해서 고친다. 핸들러가 둘 이상이면 원본을 건드리는 순간
        # 뒤 핸들러가 이미 꼬리표가 붙은 메시지를 다시 받는다.
        shown = copy.copy(record)
        shown.msg = f"{record.getMessage()} · {' '.join(_pairs(extras))}"
        shown.args = None
        return super().format(shown)


def _pairs(extras: dict[str, Any]) -> list[str]:
    rendered = []
    for key, value in extras.items():
        text = str(value)
        # 공백이 섞이면 어디까지가 값인지 안 보인다. 그때만 따옴표를 친다.
        rendered.append(f"{key}={text!r}" if not text or " " in text else f"{key}={text}")
    return rendered


def configure_logging(block_id: str, level: str = "INFO", *, as_json: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(block_id) if as_json else TextFormatter(block_id))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
