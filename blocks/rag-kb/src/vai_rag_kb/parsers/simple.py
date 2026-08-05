"""평문·PDF 파서."""

from __future__ import annotations

import logging
from io import BytesIO

from vai_rag_kb.parsers.base import BaseDocumentParser, ParsedDocument, ParseError

log = logging.getLogger(__name__)

ENCODINGS = ("utf-8", "cp949", "euc-kr", "utf-16")
"""국내 문서는 CP949로 저장된 경우가 아직 많다. UTF-8만 시도하고 실패하면
멀쩡한 약관 파일이 "읽을 수 없음"이 된다."""


class PlainTextParser(BaseDocumentParser):
    """텍스트·마크다운."""

    name = "plain"
    extensions = (".txt", ".md", ".markdown", ".csv")

    def parse(self, content: bytes, *, filename: str = "") -> ParsedDocument:
        for encoding in ENCODINGS:
            try:
                text = content.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            document = ParsedDocument(text=text, section_count=1)
            if encoding != "utf-8":
                # 어떤 인코딩으로 읽었는지 남긴다. 글자가 깨져 보일 때
                # 원인을 여기서부터 찾을 수 있다.
                document.warnings.append(f"{encoding}로 해석했다")
            if document.is_empty:
                raise ParseError("본문이 비어 있다")
            return document
        raise ParseError(f"텍스트 인코딩을 판별하지 못했다 (시도: {', '.join(ENCODINGS)})")


class PdfParser(BaseDocumentParser):
    """PDF. 텍스트 레이어가 있는 문서만 읽는다.

    **스캔 PDF는 읽지 못한다.** 이미지만 있는 문서에서 빈 텍스트를 돌려주고
    색인을 "성공"으로 끝내면, 고객사는 약관을 넣었는데 검색이 안 되는 이유를
    영영 모른다. OCR이 필요하다는 사실을 오류로 알린다.
    """

    name = "pdf"
    extensions = (".pdf",)

    def parse(self, content: bytes, *, filename: str = "") -> ParsedDocument:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - 의존성 누락은 배포 문제다
            raise ParseError(
                "PDF를 읽으려면 pypdf가 필요하다 — 에어갭 번들에 포함됐는지 확인한다"
            ) from exc

        try:
            reader = PdfReader(BytesIO(content))
        except Exception as exc:
            raise ParseError(f"PDF를 열 수 없다: {exc}") from exc

        if reader.is_encrypted:
            try:
                reader.decrypt("")  # 빈 암호로 열리는 '보기 제한' 문서가 흔하다
            except Exception as exc:
                raise ParseError("암호가 걸린 PDF다 — 암호를 푼 사본이 필요하다") from exc

        pages: list[str] = []
        for index, page in enumerate(reader.pages):
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                log.warning("PDF 페이지 추출 실패", extra={"page": index + 1})
                pages.append("")

        empty_pages = sum(1 for text in pages if not text.strip())
        document = ParsedDocument(
            text="\n\n".join(p for p in pages if p.strip()), section_count=len(pages)
        )

        if document.is_empty:
            raise ParseError("텍스트 레이어가 없다 — 스캔 PDF로 보인다. OCR을 거친 사본이 필요하다")
        if empty_pages:
            document.warnings.append(
                f"{len(pages)}쪽 중 {empty_pages}쪽에서 텍스트를 얻지 못했다 (이미지 페이지)"
            )
        return document
