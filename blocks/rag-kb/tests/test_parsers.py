"""문서 파서.

**HWP를 못 읽으면 공공 조달 입찰 자체가 막힌다.** 그리고 파서의 실패는 조용하다 —
빈 텍스트로 색인이 "성공"하면 고객사는 약관을 넣었는데 검색이 안 되는 이유를
영영 모른다. 그래서 정상 추출만큼 **실패를 실패로 알리는지**를 검증한다.
"""

from __future__ import annotations

import zipfile
import zlib
from io import BytesIO

import pytest

from vai_rag_kb import parsers
from vai_rag_kb.parsers import ParseError
from vai_rag_kb.parsers.hwp import (
    HWPTAG_PARA_TEXT,
    decode_para_text,
    extract_section_text,
)

# --- HWP 레코드 디코딩 ----------------------------------------------------
#
# CFB 컨테이너를 만드는 것은 olefile로 안 되지만(읽기 전용), 파서의 실제 로직은
# 전부 압축 해제 이후의 레코드 처리에 있다. 그 부분을 직접 검증한다.


def _record(tag_id: int, payload: bytes, level: int = 0) -> bytes:
    """레코드 하나를 만든다. 헤더는 태그(10비트)·수준(10비트)·길이(12비트)다."""
    header = tag_id | (level << 10) | (len(payload) << 20)
    return header.to_bytes(4, "little") + payload


def _para_text_record(payload: bytes) -> bytes:
    return _record(HWPTAG_PARA_TEXT, payload)


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def test_평범한_문장을_읽는다() -> None:
    text, _ = decode_para_text(_utf16("제1조 (목적) 이 약관은"))
    assert text == "제1조 (목적) 이 약관은"


def test_확장_컨트롤은_16바이트를_건너뛴다() -> None:
    """여기서 폭을 틀리면 **뒤쪽 텍스트가 통째로 깨진 문자열이 되고**,
    그 문서는 검색에서 영원히 안 나온다. 이 파서에서 가장 중요한 한 줄이다."""
    # 표 개체(11) = 확장 컨트롤. 16바이트를 차지한다.
    payload = _utf16("앞") + (11).to_bytes(2, "little") + b"\x00" * 14 + _utf16("뒤")
    text, has_table = decode_para_text(payload)
    assert text == "앞뒤"
    assert has_table


def test_확장_컨트롤을_한_글자로_세면_깨진다() -> None:
    """폭을 2바이트로 잘못 세면 무엇이 나오는지 고정해 둔다 — 회귀 시 바로 드러난다."""
    payload = _utf16("앞") + (11).to_bytes(2, "little") + b"\x00" * 14 + _utf16("뒤")
    naive = "".join(
        chr(int.from_bytes(payload[i : i + 2], "little")) for i in range(0, len(payload), 2)
    )
    assert naive != "앞뒤"  # 널 문자 7개가 끼어든다


def test_줄바꿈과_문단끝은_개행으로_남는다() -> None:
    payload = _utf16("첫줄") + (10).to_bytes(2, "little") + _utf16("둘째줄")
    text, _ = decode_para_text(payload)
    assert text == "첫줄\n둘째줄"


def test_탭은_탭으로_남고_폭은_16바이트다() -> None:
    """탭(9)은 값은 한 글자지만 폭은 확장 컨트롤과 같다 — 흔히 틀리는 지점이다."""
    payload = _utf16("가") + (9).to_bytes(2, "little") + b"\x00" * 14 + _utf16("나")
    text, _ = decode_para_text(payload)
    assert text == "가\t나"


def test_홀수_바이트로_끝나도_죽지_않는다() -> None:
    """잘린 스트림에서 인덱스 오류로 죽으면 문서 하나가 수집 블록을 멈춘다."""
    text, _ = decode_para_text(_utf16("가나") + b"\x41")
    assert text == "가나"


def test_섹션에서_본문_레코드만_모은다() -> None:
    stream = (
        _para_text_record(_utf16("제1조 목적"))
        + _record(HWPTAG_PARA_TEXT - 1, b"\x01\x02\x03\x04")  # 서식 레코드 — 섞이면 안 된다
        + _para_text_record(_utf16("제2조 정의"))
    )
    text, _ = extract_section_text(stream)
    assert text == "제1조 목적\n제2조 정의"


def test_잘린_레코드에서_앞부분은_살린다() -> None:
    """뒷부분이 깨졌다고 앞 조항까지 버리면 검색에서 통째로 사라진다."""
    stream = _para_text_record(_utf16("제1조 목적")) + _para_text_record(_utf16("제2조"))[:6]
    text, _ = extract_section_text(stream)
    assert text == "제1조 목적"


def test_확장_길이_레코드를_읽는다() -> None:
    """4095바이트를 넘는 문단은 길이가 뒤따르는 4바이트에 있다."""
    body = _utf16("가" * 3000)
    header = HWPTAG_PARA_TEXT | (0 << 10) | (0xFFF << 20)
    stream = header.to_bytes(4, "little") + len(body).to_bytes(4, "little") + body
    text, _ = extract_section_text(stream)
    assert text == "가" * 3000


def test_HWP가_아니면_거부한다() -> None:
    with pytest.raises(ParseError, match="CFB"):
        parsers.by_name("hwp").parse(b"not an ole file", filename="x.hwp")


# --- HWPX / DOCX ---------------------------------------------------------


def _hwpx(paragraphs: list[str]) -> bytes:
    ns = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    body = "".join(f"<hp:p><hp:run><hp:t>{p}</hp:t></hp:run></hp:p>" for p in paragraphs)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", f"<hp:sec {ns}>{body}</hp:sec>")
    return buffer.getvalue()


def _docx(paragraphs: list[str], *, with_header: bool = False) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>"
        )
        if with_header:
            archive.writestr("word/header1.xml", f"<w:hdr {ns}></w:hdr>")
    return buffer.getvalue()


def test_HWPX_문단을_읽는다() -> None:
    document = parsers.parse(_hwpx(["제1조 (목적)", "제2조 (정의)"]), filename="약관.hwpx")
    assert document.text == "제1조 (목적)\n제2조 (정의)"


def test_문단_경계를_살린다() -> None:
    """전부 이어 붙이면 조항 경계 청킹이 '제2조'를 앞 문장 꼬리로 보고
    한 덩어리에 밀어 넣는다."""
    document = parsers.parse(_hwpx(["가", "나"]), filename="x.hwpx")
    assert "\n" in document.text


def test_네임스페이스가_달라도_읽는다() -> None:
    """한글 버전마다 네임스페이스 URI가 바뀐다. 거기 묶이면 특정 버전에서만 도는
    파서가 되는데, 고객사가 어느 버전을 쓸지 우리는 모른다."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Contents/section0.xml",
            '<sec xmlns="http://example.invalid/other/version"><p><t>본문</t></p></sec>',
        )
    assert parsers.parse(buffer.getvalue(), filename="x.hwpx").text == "본문"


def test_DOCX_문단을_읽는다() -> None:
    document = parsers.parse(_docx(["약관 본문", "둘째 문단"]), filename="terms.docx")
    assert document.text == "약관 본문\n둘째 문단"


def test_머리말이_있으면_알린다() -> None:
    """약관에서는 조항 번호가 머리말에 있는 경우가 있다."""
    document = parsers.parse(_docx(["본문"], with_header=True), filename="x.docx")
    assert any("머리말" in w for w in document.warnings)


def test_빈_DOCX는_실패로_알린다() -> None:
    with pytest.raises(ParseError, match="추출하지 못했다"):
        parsers.parse(_docx([]), filename="empty.docx")


def test_zip이_아니면_거부한다() -> None:
    with pytest.raises(ParseError, match="zip"):
        parsers.by_name("docx").parse(b"PK\x03\x04broken", filename="x.docx")


# --- 평문 ---------------------------------------------------------------


def test_CP949_문서를_읽는다() -> None:
    """국내 문서는 CP949로 저장된 경우가 아직 많다. UTF-8만 시도하면
    멀쩡한 약관이 '읽을 수 없음'이 된다."""
    document = parsers.parse("제1조 목적".encode("cp949"), filename="약관.txt")
    assert document.text == "제1조 목적"
    assert any("cp949" in w for w in document.warnings)


def test_UTF8은_경고_없이_읽는다() -> None:
    assert parsers.parse("본문".encode(), filename="x.txt").warnings == []


# --- 형식 판별 -----------------------------------------------------------


def test_확장자가_틀려도_내용으로_판별한다() -> None:
    """고객사 파일은 확장자가 자주 틀리다(HWPX를 .hwp로 저장). 확장자만 믿으면
    멀쩡한 문서가 형식 오류로 반려되고, 고객사는 그것을 제품 결함으로 본다."""
    assert parsers.sniff(_hwpx(["본문"]), "잘못된이름.hwp").name == "hwpx"
    assert parsers.sniff(_docx(["본문"]), "문서.hwp").name == "docx"


def test_PDF는_매직_바이트로_잡는다() -> None:
    assert parsers.sniff(b"%PDF-1.7\n...", "no-extension").name == "pdf"


def test_CFB는_HWP로_본다() -> None:
    assert parsers.sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "x.hwp").name == "hwp"


def test_판별_불가는_평문으로_떨어진다() -> None:
    assert parsers.sniff(b"just some text", "").name == "plain"


def test_파서를_직접_지정할_수_있다() -> None:
    """자동 판별이 틀렸을 때 운영자가 우회할 통로를 남긴다."""
    assert parsers.parse(b"raw", filename="x.hwp", parser="plain").text == "raw"


def test_알_수_없는_파서는_거부한다() -> None:
    with pytest.raises(ValueError, match="알 수 없는"):
        parsers.parse(b"x", parser="nonexistent")


def test_지원_형식_목록() -> None:
    assert set(parsers.available()) == {"plain", "hwp", "hwpx", "docx", "pdf"}


# --- 방어 ---------------------------------------------------------------


def test_zip_bomb을_막는다() -> None:
    """수집 블록의 메모리를 터뜨리면 지식베이스 전체가 멈춘다."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\x00" * (600 * 1024 * 1024))
    with pytest.raises(ParseError, match="상한"):
        parsers.by_name("docx").parse(buffer.getvalue(), filename="bomb.docx")


def test_압축되지_않은_섹션도_읽는다() -> None:
    """압축 플래그가 꺼진 문서도 있다. zlib 오류로 죽으면 안 된다."""
    from vai_rag_kb.parsers.hwp import _decompress

    plain = _para_text_record(_utf16("본문"))
    assert _decompress(plain) == plain
    assert _decompress(zlib.compress(plain)[2:-4]) == plain


# --- PDF ----------------------------------------------------------------


def _pdf(lines: list[str]) -> bytes:
    """텍스트 레이어가 있는 PDF를 만든다 — 파서와 같은 라이브러리를 쓰지 않는다."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)

    # 폰트 자원이 없으면 pypdf가 텍스트를 뽑지 못한다 — 실제 PDF에는 항상 있다.
    font = DictionaryObject()
    font.update(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = writer._add_object(font)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources

    stream = DecodedStreamObject()
    body = "\n".join(f"BT /F1 12 Tf 50 {780 - i * 20} Td ({t}) Tj ET" for i, t in enumerate(lines))
    stream.set_data(body.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_PDF_텍스트_레이어를_읽는다() -> None:
    document = parsers.parse(_pdf(["Article 1 Purpose", "Article 2 Definitions"]), filename="t.pdf")
    assert "Article 1 Purpose" in document.text
    assert "Article 2 Definitions" in document.text


def test_스캔_PDF는_OCR이_필요하다고_알린다() -> None:
    """이미지만 있는 문서에서 빈 텍스트로 색인을 '성공' 처리하면, 고객사는
    약관을 넣었는데 검색이 안 되는 이유를 영영 모른다."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = BytesIO()
    writer.write(buffer)
    with pytest.raises(ParseError, match="OCR"):
        parsers.parse(buffer.getvalue(), filename="scan.pdf")


def test_깨진_PDF는_실패로_알린다() -> None:
    with pytest.raises(ParseError):
        parsers.by_name("pdf").parse(b"%PDF-1.7\nbroken", filename="x.pdf")
