"""운영 통계와 콜 이력.

폐쇄망 고객사는 "잘 돌고 있나"를 **스스로** 답할 수 있어야 한다. 공급사가
원격으로 들여다볼 수 없으므로, 지표가 없으면 판단의 근거도 없고 증설·튜닝
요청은 전부 체감에 의존하게 된다.

집계 단위는 **하루 + 테넌트**다. 더 잘게 쪼개면 보관량이 늘고, 더 크게 묶으면
"어제부터 이상하다"를 못 잡는다.

여기 담기는 것은 **마스킹된 텍스트뿐**이다(``filter.clean`` 산출물). 원문은
어디에도 남지 않는다 — 이력 조회 화면은 개인정보 열람 창구가 되기 가장 쉬운
자리이고, 한 번 새면 회수할 방법이 없다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from vai_contracts.session import SessionProfile


class DailyStats(BaseModel):
    """하루치 집계 한 칸.

    합계와 개수를 **나눠서** 담는다. 평균만 저장하면 기간을 합칠 때 다시
    평균의 평균이 되고, 그 값은 통화 수가 다른 날들 사이에서 틀린다.
    """

    day: str
    """``YYYY-MM-DD`` (UTC)."""

    sessions: int = 0
    sessions_meeting: int = 0
    sessions_aicc: int = 0

    utterances: int = 0
    """확정 인식 발화 수. 중간 결과는 세지 않는다 — 같은 말이 여러 번 잡힌다."""

    audio_ms: int = 0

    confidence_sum: float = 0.0
    confidence_n: int = 0

    pii_masked: int = 0
    """개인정보가 지워진 발화 수. **0이 계속되면 마스킹이 꺼진 것을 의심한다.**"""

    rule_hits: int = 0

    bot_turns: int = 0
    handoffs: int = 0
    """사람에게 넘긴 횟수. 봇 성적표의 분모가 아니라 분자다."""

    summaries_ready: int = 0
    summaries_failed: int = 0
    summary_latency_sum: int = 0
    summary_latency_n: int = 0
    summary_waited_sum: int = 0
    """실시간 인식에 양보하며 기다린 총 시간(ms). 커지면 장비가 모자란다."""

    summary_gave_up: int = 0

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / self.confidence_n if self.confidence_n else 0.0

    @property
    def mean_summary_latency_ms(self) -> float:
        n = self.summary_latency_n
        return self.summary_latency_sum / n if n else 0.0

    @property
    def handoff_rate(self) -> float:
        return self.handoffs / self.bot_turns if self.bot_turns else 0.0


class RankEntry(BaseModel):
    key: str
    label: str = ""
    count: int = 0


class FallbackUtterance(BaseModel):
    """봇이 못 알아들어 사람에게 넘어가기 **직전** 고객 발화.

    재학습 후보다. 무엇을 물었는데 못 알아들었는지가 인텐트 보강의 유일한
    1차 자료이며, 이것 없이 인텐트를 늘리는 것은 추측이다.
    """

    text: str
    node_id: str = ""
    session_id: str = ""
    count: int = 1
    """같은 말이 반복되면 우선순위가 높다."""

    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CallTurn(BaseModel):
    """이력에 남는 한 마디. **마스킹본이다.**"""

    at_ms: int = 0
    speaker: str = ""
    text: str = ""
    is_bot: bool = False


class CallRecord(BaseModel):
    """콜(또는 회의) 한 건의 이력.

    감사 로그와 다르다. 감사 로그는 "누가 무엇을 했나"이고 이건 "무슨 대화가
    오갔나"다. 둘을 한 저장소에 섞으면 보관 기간과 열람 권한이 서로 다른
    데이터가 한 덩어리가 된다.
    """

    session_id: str
    tenant_id: str
    profile: SessionProfile = SessionProfile.AICC
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    duration_ms: int = 0

    utterances: int = 0
    pii_masked: int = 0
    rule_hits: list[str] = Field(default_factory=list)
    bot_turns: int = 0
    handed_off: bool = False

    summary_status: str = ""
    turns: list[CallTurn] = Field(default_factory=list)
    truncated: bool = False
    """발화가 상한을 넘어 일부만 남았는가. 잘렸다는 사실을 숨기면 이력을
    증거로 쓸 수 없다."""


class StatsOverview(BaseModel):
    """대시보드 상단 숫자판."""

    tenant_id: str
    days: int = 7
    since: str = ""
    until: str = ""

    sessions: int = 0
    utterances: int = 0
    mean_confidence: float = 0.0
    pii_masked: int = 0
    rule_hits: int = 0
    handoffs: int = 0
    handoff_rate: float = 0.0
    summaries_ready: int = 0
    summaries_failed: int = 0
    summary_success_rate: float = 0.0
    mean_summary_latency_ms: float = 0.0
    summary_gave_up: int = 0
    daily: list[DailyStats] = Field(default_factory=list)
