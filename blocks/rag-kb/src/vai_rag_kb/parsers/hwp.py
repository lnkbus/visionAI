"""HWP 5.0 파서 — 공공 조달의 관문.

공공기관 약관·지침·매뉴얼은 대부분 HWP다. 이 형식을 못 읽으면 "문서를 넣어
보세요"라는 PoC 첫 단계에서 막히고, 입찰 자체가 성립하지 않는다.

HWP 5.0은 CFB(복합 파일 이진, MS 구조화 저장소) 컨테이너다:

* ``FileHeader``          — 시그니처·버전·압축/암호 플래그
* ``BodyText/Section*``   — 본문. 보통 raw deflate로 압축돼 있다
* ``ViewText/Section*``   — **배포용 문서**의 본문. 복호화 키가 필요하다

압축을 풀면 레코드 스트림이 나온다. 헤더 4바이트에 태그·수준·길이가 비트로
채워져 있고, 본문은 ``HWPTAG_PARA_TEXT`` 레코드에 UTF-16LE로 들어 있다.

**제어 문자 폭을 정확히 건너뛰는 것이 이 파서의 전부다.** 확장 컨트롤은
16바이트를 차지하는데 한 글자로 취급하면 그 뒤의 텍스트가 통째로 깨진 문자열이
되고, 그 문서는 검색에서 영원히 안 나온다.
"""

from __future__ import annotations

import io
import logging
import zlib

from vai_rag_kb.parsers.base import BaseDocumentParser, ParsedDocument, ParseError

log = logging.getLogger(__name__)

SIGNATURE = b"HWP Document File"

HWPTAG_BEGIN = 0x10
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51
"""본문 텍스트 레코드. 다른 태그는 서식·표 구조라 본문 추출에 쓰지 않는다."""

EXTENDED_SIZE = 0xFFF
"""헤더의 길이 필드가 이 값이면 실제 길이가 뒤따르는 4바이트에 있다."""

# 제어 문자 분류 (HWP 5.0 명세). 폭을 틀리면 뒤쪽 텍스트가 통째로 어긋난다.
CHAR_CONTROLS = frozenset({0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31})
"""1워드를 차지한다. 10=줄바꿈, 13=문단 끝."""

WIDE_CONTROLS = frozenset(
    {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}
)
"""**8워드(16바이트)** 를 차지한다. 표·그림·각주 같은 개체가 여기 들어간다."""

CONTROL_TEXT = {9: "\t", 10: "\n", 13: "\n"}
"""본문에 남길 제어 문자. 나머지는 버린다 — 개체 자리표시자를 남기면
청킹이 그것을 문장으로 오인한다."""

TABLE_CONTROL = 11
"""표 개체. 표 안의 글자는 별도 레코드에 있어 이 파서로는 못 읽는다."""


def _decompress(data: bytes) -> bytes:
    """raw deflate 해제. HWP는 zlib 헤더 없이 저장한다."""
    try:
        return zlib.decompress(data, -15)
    except zlib.error:
        # 압축 플래그가 꺼진 문서도 있다. 원본 그대로 시도한다.
        return data


def iter_records(data: bytes) -> list[tuple[int, int, bytes]]:
    """레코드 스트림을 ``(tag_id, level, payload)``로 푼다."""
    records: list[tuple[int, int, bytes]] = []
    offset = 0
    total = len(data)
    while offset + 4 <= total:
        header = int.from_bytes(data[offset : offset + 4], "little")
        offset += 4
        tag_id = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == EXTENDED_SIZE:
            if offset + 4 > total:
                break
            size = int.from_bytes(data[offset : offset + 4], "little")
            offset += 4
        if offset + size > total:
            # 잘린 스트림. 여기까지 읽은 것은 살린다 — 뒷부분이 깨졌다고
            # 앞부분 조항까지 버리면 검색에서 통째로 사라진다.
            log.warning("HWP 레코드가 잘렸다", extra={"tag_id": tag_id, "offset": offset})
            break
        records.append((tag_id, level, data[offset : offset + size]))
        offset += size
    return records


def decode_para_text(payload: bytes) -> tuple[str, bool]:
    """``HWPTAG_PARA_TEXT`` 한 건을 문자열로. ``(텍스트, 표_포함_여부)``.

    UTF-16LE 코드 유닛을 하나씩 보며 제어 문자의 **폭만큼** 건너뛴다.
    확장 컨트롤(16바이트)을 한 글자로 세면 그 뒤가 전부 어긋난다.
    """
    out: list[str] = []
    has_table = False
    index = 0
    limit = len(payload) - (len(payload) % 2)
    while index < limit:
        code = int.from_bytes(payload[index : index + 2], "little")
        if code in CHAR_CONTROLS:
            out.append(CONTROL_TEXT.get(code, ""))
            index += 2
        elif code in WIDE_CONTROLS:
            if code == TABLE_CONTROL:
                has_table = True
            out.append(CONTROL_TEXT.get(code, ""))
            index += 16
        else:
            out.append(chr(code))
            index += 2
    return "".join(out), has_table


def extract_section_text(data: bytes) -> tuple[str, bool]:
    """섹션 스트림(압축 해제 후)에서 본문을 뽑는다."""
    parts: list[str] = []
    has_table = False
    for tag_id, _level, payload in iter_records(data):
        if tag_id != HWPTAG_PARA_TEXT:
            continue
        text, table = decode_para_text(payload)
        has_table = has_table or table
        if text:
            parts.append(text)
    return "\n".join(parts), has_table


class HwpParser(BaseDocumentParser):
    """HWP 5.0 (한글 워드프로세서) 이진 문서."""

    name = "hwp"
    extensions = (".hwp",)

    def parse(self, content: bytes, *, filename: str = "") -> ParsedDocument:
        try:
            import olefile
        except ImportError as exc:  # pragma: no cover - 의존성 누락은 배포 문제다
            raise ParseError(
                "HWP를 읽으려면 olefile이 필요하다 — 에어갭 번들에 포함됐는지 확인한다"
            ) from exc

        if not olefile.isOleFile(io.BytesIO(content)):
            raise ParseError("HWP 형식이 아니다 (CFB 컨테이너가 아님)")

        ole = olefile.OleFileIO(io.BytesIO(content))
        try:
            return self._read(ole)
        finally:
            ole.close()

    def _read(self, ole: object) -> ParsedDocument:
        entries = {"/".join(path) for path in ole.listdir()}  # type: ignore[attr-defined]

        if "FileHeader" not in entries:
            raise ParseError("FileHeader가 없다 — 손상됐거나 HWP가 아니다")
        header = ole.openstream("FileHeader").read()  # type: ignore[attr-defined]
        if not header.startswith(SIGNATURE):
            raise ParseError("HWP 시그니처가 맞지 않는다")

        warnings: list[str] = []
        properties = int.from_bytes(header[36:40], "little") if len(header) >= 40 else 0
        if properties & 0x02:
            # 암호가 걸린 문서는 키 없이 못 읽는다. 빈 텍스트로 "성공" 처리하면
            # 고객사는 문서를 넣었는데 검색이 안 되는 이유를 영영 모른다.
            raise ParseError("암호가 걸린 HWP다 — 암호를 푼 사본이 필요하다")

        sections = sorted(name for name in entries if name.startswith("BodyText/Section"))
        if not sections:
            if any(name.startswith("ViewText/") for name in entries):
                raise ParseError(
                    "배포용 HWP다(ViewText) — 복호화 키가 필요해 읽을 수 없다. 원본 문서를 요청한다"
                )
            raise ParseError("BodyText 섹션이 없다")

        parts: list[str] = []
        has_table = False
        for name in sections:
            raw = ole.openstream(name).read()  # type: ignore[attr-defined]
            text, table = extract_section_text(_decompress(raw))
            has_table = has_table or table
            if text.strip():
                parts.append(text)

        if has_table:
            # 표 안의 글자는 별도 레코드에 있어 이 파서로는 안 잡힌다.
            # 조용히 넘기면 "왜 이 조항이 검색이 안 되지"를 아무도 설명하지 못한다.
            warnings.append("표가 포함된 문서다 — 표 안의 글자는 추출되지 않았다")

        document = ParsedDocument(
            text="\n\n".join(parts), section_count=len(sections), warnings=warnings
        )
        if document.is_empty:
            raise ParseError("본문 텍스트를 추출하지 못했다 (그림·표만 있는 문서일 수 있다)")
        return document
