"""zip+XML 문서 파서 — HWPX(OWPML)와 DOCX(OOXML).

두 형식 모두 "XML을 담은 zip"이라 **표준 라이브러리만으로** 읽힌다.
에어갭 반입에서 의존성 하나는 검토 대상 하나다. 줄일 수 있으면 줄인다.

네임스페이스를 무시하고 **지역명(local name)** 으로만 태그를 찾는다. 한글·워드
버전에 따라 네임스페이스 URI가 바뀌는데, 거기에 묶으면 특정 버전에서만 도는
파서가 된다 — 고객사가 어느 버전으로 문서를 만들지 우리는 모른다.
"""

from __future__ import annotations

import logging
import zipfile
from io import BytesIO
from xml.etree import ElementTree

from vai_rag_kb.parsers.base import BaseDocumentParser, ParsedDocument, ParseError

log = logging.getLogger(__name__)

MAX_UNCOMPRESSED = 512 * 1024 * 1024
"""압축 해제 상한. zip bomb으로 수집 블록의 메모리를 터뜨릴 수 있다."""


def _local(tag: str) -> str:
    """``{ns}p`` → ``p``."""
    return tag.rsplit("}", 1)[-1]


def _open_zip(content: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ParseError("zip 컨테이너가 아니다 — 형식이 맞는지 확인한다") from exc

    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_UNCOMPRESSED:
        raise ParseError(f"압축 해제 크기가 상한을 넘는다 ({total // 1048576}MB)")
    return archive


def _text_from_xml(
    payload: bytes, *, text_tag: str, paragraph_tag: str, break_tags: tuple[str, ...] = ()
) -> str:
    """문단 단위로 텍스트를 모은다.

    문단 경계를 살리는 것이 중요하다. 전부 이어 붙이면 조항 경계 청킹이
    "제1조"를 앞 문장 꼬리로 보고 한 덩어리에 밀어 넣는다.
    """
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ParseError(f"XML을 해석할 수 없다: {exc}") from exc

    paragraphs: list[str] = []
    buffer: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        name = _local(node.tag)
        if name == paragraph_tag and buffer:
            paragraphs.append("".join(buffer))
            buffer.clear()
        if name == text_tag and node.text:
            buffer.append(node.text)
        elif name in break_tags:
            buffer.append("\n")
        for child in node:
            walk(child)

    walk(root)
    if buffer:
        paragraphs.append("".join(buffer))
    return "\n".join(p for p in paragraphs if p.strip())


class HwpxParser(BaseDocumentParser):
    """HWPX (OWPML) — 한글 2014 이후의 개방 형식.

    HWP 이진보다 훨씬 안정적으로 읽힌다. 고객사가 형식을 고를 수 있다면
    이쪽을 권한다 — 표 안의 글자까지 그대로 잡힌다.
    """

    name = "hwpx"
    extensions = (".hwpx",)

    def parse(self, content: bytes, *, filename: str = "") -> ParsedDocument:
        archive = _open_zip(content)
        with archive:
            sections = sorted(
                name
                for name in archive.namelist()
                if name.startswith("Contents/section") and name.endswith(".xml")
            )
            if not sections:
                raise ParseError("HWPX 본문(Contents/section*.xml)이 없다")

            parts = [
                _text_from_xml(
                    archive.read(name), text_tag="t", paragraph_tag="p", break_tags=("lineBreak",)
                )
                for name in sections
            ]

        document = ParsedDocument(
            text="\n\n".join(p for p in parts if p.strip()), section_count=len(sections)
        )
        if document.is_empty:
            raise ParseError("본문 텍스트를 추출하지 못했다")
        return document


class DocxParser(BaseDocumentParser):
    """DOCX (OOXML). 금융권 약관·내규에서 가장 흔하다."""

    name = "docx"
    extensions = (".docx",)

    def parse(self, content: bytes, *, filename: str = "") -> ParsedDocument:
        archive = _open_zip(content)
        with archive:
            if "word/document.xml" not in archive.namelist():
                raise ParseError("DOCX 본문(word/document.xml)이 없다 — .doc(구형)일 수 있다")
            body = _text_from_xml(
                archive.read("word/document.xml"),
                text_tag="t",
                paragraph_tag="p",
                break_tags=("br", "tab"),
            )
            warnings: list[str] = []
            # 머리말·꼬리말은 별도 파트다. 약관에서는 조항 번호가 여기 있는 경우가 있다.
            if any(name.startswith("word/header") for name in archive.namelist()):
                warnings.append("머리말/꼬리말은 추출하지 않았다")

        document = ParsedDocument(text=body, section_count=1, warnings=warnings)
        if document.is_empty:
            raise ParseError("본문 텍스트를 추출하지 못했다")
        return document
