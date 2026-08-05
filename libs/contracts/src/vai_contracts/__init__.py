"""VisionAI 공유 계약 (v1).

블록 간에 공유가 허용되는 유일한 코드다. 블록끼리 서로를 import 하는 것은
CI(import-linter)가 차단한다 — 결합은 반드시 이 계약을 통해서만 이뤄진다.
"""

from vai_contracts.block import BlockManifest, BlockTier, CapacityUnit
from vai_contracts.events import (
    SCHEMA_VERSION,
    AssistPopup,
    AudioChunk,
    AudioSegment,
    BaseEvent,
    FilterResult,
    KnowledgeHit,
    MatchedRule,
    SessionClosed,
    SttDelta,
)
from vai_contracts.session import (
    AudioFormat,
    ChannelRole,
    Session,
    SessionCreate,
    SessionProfile,
    SessionState,
)
from vai_contracts.topics import Topic, stream, ui_channel
from vai_contracts.ws import (
    AgentAssistUpdate,
    AudioDataMessage,
    CloseSessionMessage,
    CompliancePayload,
    ErrorMessage,
    SttResultPayload,
)

__all__ = [
    "SCHEMA_VERSION",
    "AgentAssistUpdate",
    "AssistPopup",
    "AudioChunk",
    "AudioDataMessage",
    "AudioFormat",
    "AudioSegment",
    "BaseEvent",
    "BlockManifest",
    "BlockTier",
    "CapacityUnit",
    "ChannelRole",
    "CloseSessionMessage",
    "CompliancePayload",
    "ErrorMessage",
    "FilterResult",
    "KnowledgeHit",
    "MatchedRule",
    "Session",
    "SessionClosed",
    "SessionCreate",
    "SessionProfile",
    "SessionState",
    "SttDelta",
    "SttResultPayload",
    "Topic",
    "stream",
    "ui_channel",
]
