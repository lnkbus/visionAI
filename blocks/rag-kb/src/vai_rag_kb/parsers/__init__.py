"""문서 파서 레지스트리.

확장자로 고르되, **내용의 매직 바이트를 우선한다.** 고객사에서 오는 파일은
확장자가 자주 틀린다(HWPX를 .hwp로 저장, DOCX를 .doc로 내려받기). 확장자만
믿으면 멀쩡한 문서가 "형식 오류"로 반려된다.
"""

from __future__ import annotations

import logging

from vai_rag_kb.parsers.base import BaseDocumentParser, ParsedDocument, ParseError
from vai_rag_kb.parsers.hwp import HwpParser
from vai_rag_kb.parsers.simple import PdfParser, PlainTextParser
from vai_rag_kb.parsers.xmlzip import DocxParser, HwpxParser

log = logging.getLogger(__name__)

_PARSERS: tuple[BaseDocumentParser, ...] = (
    PlainTextParser(),
    HwpParser(),
    HwpxParser(),
    DocxParser(),
    PdfParser(),
)

_BY_NAME = {parser.name: parser for parser in _PARSERS}

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
"""CFB 컨테이너. HWP 5.0과 구형 .doc/.xls가 공유한다."""

ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF"


def available() -> list[str]:
    return sorted(_BY_NAME)


def by_name(name: str) -> BaseDocumentParser:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"알 수 없는 파서 '{name}'. 사용 가능: {available()}") from None


def sniff(content: bytes, filename: str = "") -> BaseDocumentParser:
    """내용과 파일명으로 파서를 고른다.

    내용을 먼저 보는 이유: 고객사 파일은 확장자가 자주 틀리다. 확장자만 믿으면
    멀쩡한 문서가 형식 오류로 반려되고, 그 반려를 고객사는 제품 결함으로 본다.
    """
    if content.startswith(PDF_MAGIC):
        return _BY_NAME["pdf"]
    if content.startswith(OLE_MAGIC):
        # CFB 안에 뭐가 들었는지는 파서가 시그니처로 확인한다.
        return _BY_NAME["hwp"]
    if content.startswith(ZIP_MAGIC):
        # HWPX와 DOCX는 둘 다 zip이다. 내용물 이름으로 가른다.
        return _BY_NAME["hwpx"] if _looks_like_hwpx(content) else _BY_NAME["docx"]

    for parser in _PARSERS:
        if filename and parser.supports(filename):
            return parser
    return _BY_NAME["plain"]


def _looks_like_hwpx(content: bytes) -> bool:
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return False
    return any(name.startswith("Contents/") for name in names)


def parse(content: bytes, *, filename: str = "", parser: str = "") -> ParsedDocument:
    """문서를 텍스트로. 실패는 :class:`ParseError`로 올린다.

    빈 문자열을 돌려주고 색인을 "성공"으로 끝내지 않는다 — 고객사는 문서를
    넣었는데 검색이 안 되는 이유를 영영 모르게 된다.
    """
    chosen = by_name(parser) if parser else sniff(content, filename)
    document = chosen.parse(content, filename=filename)
    log.info(
        "문서 파싱",
        extra={
            "parser": chosen.name,
            # 'filename'은 LogRecord 예약어라 그대로 쓰면 로깅이 KeyError로 죽는다.
            # 파싱에 **성공할 때마다** 터지는 종류의 사고다.
            "source_name": filename,
            "chars": len(document.text),
            "warnings": len(document.warnings),
        },
    )
    return document


__all__ = [
    "BaseDocumentParser",
    "ParseError",
    "ParsedDocument",
    "available",
    "by_name",
    "parse",
    "sniff",
]
