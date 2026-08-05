"""온라인 화자 군집화.

회의는 참석자 수를 미리 모른다. 그래서 고정 클러스터가 아니라 **점진적
군집화**를 쓴다: 새 발화의 임베딩을 기존 화자 중심과 비교해, 충분히 가까우면
그 화자로 보고 아니면 새 화자를 만든다.

버스·네트워크와 무관한 순수 상태 기계로 분리했다. 화자를 잘못 가르면 회의록의
"누가 무엇을 말했는가"가 통째로 틀리므로, 경계 조건을 단위 테스트로 고정한다.

**임계값이 이 블록의 전부다.** 높게 잡으면 한 사람이 여러 화자로 쪼개지고,
낮게 잡으면 여러 사람이 한 화자로 뭉친다. 회의록에서는 쪼개지는 쪽이 낫다 —
사람이 합치는 편이 나누는 것보다 쉽다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class DiarizerConfig:
    similarity_threshold: float = 0.72
    """이보다 가까우면 같은 화자로 본다. 임베더를 바꾸면 반드시 재조정해야 한다."""

    max_speakers: int = 12
    """상한. 넘으면 가장 가까운 화자에 붙인다 — 회의에 20명이 잡히면
    분리가 실패한 것이지 실제로 20명인 경우는 드물다."""

    min_duration_ms: int = 700
    """이보다 짧은 발화로는 **새 화자를 만들지 않는다.** "네", "음" 같은
    맞장구가 매번 새 참석자가 되는 것을 막는다. 기존 화자 배정은 허용한다."""

    centroid_momentum: float = 0.15
    """중심 갱신 비율. 크면 최근 발화에 민감해지고, 작으면 초기 발화에 고착된다."""


@dataclass
class Speaker:
    speaker_id: str
    centroid: np.ndarray
    segments: int = 1
    total_ms: int = 0

    def absorb(self, embedding: np.ndarray, duration_ms: int, momentum: float) -> None:
        """중심을 갱신한다. 지수이동평균이라 옛 발화의 영향이 서서히 줄어든다."""
        updated = (1 - momentum) * self.centroid + momentum * embedding
        norm = float(np.linalg.norm(updated))
        if norm > 0:
            self.centroid = (updated / norm).astype(np.float32)
        self.segments += 1
        self.total_ms += duration_ms


@dataclass
class Assignment:
    speaker_id: str
    confidence: float
    is_new: bool


@dataclass
class OnlineDiarizer:
    """세션 하나의 화자 군집."""

    config: DiarizerConfig = field(default_factory=DiarizerConfig)
    speakers: list[Speaker] = field(default_factory=list)
    _counter: int = 0

    @property
    def speaker_count(self) -> int:
        return len(self.speakers)

    def assign(self, embedding: np.ndarray, duration_ms: int) -> Assignment | None:
        """발화 구간을 화자에 배정한다. 판정할 수 없으면 ``None``."""
        if embedding is None or embedding.size == 0:
            return None

        best, similarity = self._nearest(embedding)

        if best is not None and similarity >= self.config.similarity_threshold:
            best.absorb(embedding, duration_ms, self.config.centroid_momentum)
            return Assignment(best.speaker_id, similarity, is_new=False)

        if duration_ms < self.config.min_duration_ms:
            # 짧은 발화로는 새 화자를 만들지 않는다. 기존 화자가 있으면 가장
            # 가까운 쪽에 붙이고, 아무도 없으면 판정을 미룬다.
            if best is None:
                return None
            best.absorb(embedding, duration_ms, self.config.centroid_momentum * 0.5)
            return Assignment(best.speaker_id, similarity, is_new=False)

        if len(self.speakers) >= self.config.max_speakers:
            if best is None:  # pragma: no cover - max_speakers>0이면 도달하지 않는다
                return None
            log.warning(
                "화자 상한 도달 — 가장 가까운 화자에 배정",
                extra={
                    "max_speakers": self.config.max_speakers,
                    "similarity": round(similarity, 3),
                },
            )
            best.absorb(embedding, duration_ms, self.config.centroid_momentum)
            return Assignment(best.speaker_id, similarity, is_new=False)

        speaker = self._create(embedding, duration_ms)
        return Assignment(speaker.speaker_id, similarity, is_new=True)

    def _nearest(self, embedding: np.ndarray) -> tuple[Speaker | None, float]:
        if not self.speakers:
            return None, 0.0
        # 임베딩은 정규화되어 있으므로 내적이 곧 코사인 유사도다.
        similarities = np.array([float(speaker.centroid @ embedding) for speaker in self.speakers])
        index = int(np.argmax(similarities))
        return self.speakers[index], float(similarities[index])

    def _create(self, embedding: np.ndarray, duration_ms: int) -> Speaker:
        self._counter += 1
        speaker = Speaker(
            speaker_id=f"speaker_{self._counter}",
            centroid=embedding.astype(np.float32),
            total_ms=duration_ms,
        )
        self.speakers.append(speaker)
        log.info("새 화자 감지", extra={"speaker_id": speaker.speaker_id})
        return speaker

    def summary(self) -> list[dict[str, object]]:
        """참석자 요약. 발언량 순으로 정렬해 회의록 참석자 목록의 근거가 된다."""
        return [
            {
                "speaker_id": speaker.speaker_id,
                "segments": speaker.segments,
                "total_ms": speaker.total_ms,
            }
            for speaker in sorted(self.speakers, key=lambda s: s.total_ms, reverse=True)
        ]
