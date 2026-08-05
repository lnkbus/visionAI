"""문서 파서 계약.

약관·매뉴얼은 평문으로 오지 않는다. 공공은 HWP/HWPX, 금융은 PDF/DOCX가
대부분이고, 특히 **HWP를 못 읽으면 공공 조달 입찰 자체가 막힌다.**

파서도 Model-Agnostic 원칙을 따른다. 서비스 로직(app.py)은 이 ABC와
레지스트리만 알고 구체 파서를 직접 import 하지 않는다 — 파서 교체가
수집 로직 수정으로 번지면 "조립 가능한 블록"이 성립하지 않는다.

**추출 실패를 성공으로 위장하지 않는다.** 빈 문자열을 돌려주고 색인이
"성공"으로 끝나면, 고객사는 문서를 넣었는데 검색이 안 되는 이유를 영영 모른다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ParseError(RuntimeError):
    """문서를 읽을 수 없다. 색인을 시작하지 않는다."""


@dataclass
class ParsedDocument:
    text: str
    title: str = ""
    """문서 내부 메타데이터에서 뽑은 제목. 없으면 빈 문자열."""

    section_count: int = 0
    warnings: list[str] = field(default_factory=list)
    """읽기는 했지만 온전하지 않은 부분. 표·수식·이미지 속 글자 등.
    조용히 넘기면 "왜 이 조항이 검색이 안 되지"를 아무도 설명하지 못한다."""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class BaseDocumentParser(ABC):
    name: str
    extensions: tuple[str, ...]

    @abstractmethod
    def parse(self, content: bytes, *, filename: str = "") -> ParsedDocument:
        """바이트에서 본문 텍스트를 뽑는다. 실패하면 :class:`ParseError`."""

    def supports(self, filename: str) -> bool:
        lowered = filename.lower()
        return any(lowered.endswith(ext) for ext in self.extensions)
