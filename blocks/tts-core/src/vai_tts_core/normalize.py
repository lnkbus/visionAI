"""한국어 TTS 전처리 — 숫자·기호를 읽는 대로 바꾼다.

**이 모듈이 없으면 한국어 음성봇은 쓸 수 없다.** 대부분의 TTS 엔진은 숫자를
글자 그대로 흘리거나 영어식으로 읽는다. "50,000원"이 "오영영영영원"으로 나오면
고객은 금액을 알아듣지 못하고, 그 통화는 실패한 통화다.

가장 흔한 실수는 **고유어 수사와 한자어 수사의 구분**이다:

* ``3개`` → "세 개"   (고유어)  ← "삼 개"는 어색하다
* ``3분`` → "삼 분"   (한자어)  ← "세 분"은 사람 세 명이 된다
* ``3시 30분`` → "세 시 삼십 분"  (한 문장에 둘이 섞인다)

엔진 바깥에 두는 이유: 엔진을 바꿔도 읽는 방식은 그대로여야 한다. 고객사가
"금액을 이렇게 읽어 달라"고 요구할 때 엔진 교체와 무관하게 대응할 수 있다.
"""

from __future__ import annotations

import re

SINO_DIGITS = ("영", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구")
SINO_SMALL_UNITS = ("", "십", "백", "천")
SINO_BIG_UNITS = ("", "만", "억", "조", "경")

NATIVE_ATTRIBUTIVE = {
    1: "한",
    2: "두",
    3: "세",
    4: "네",
    5: "다섯",
    6: "여섯",
    7: "일곱",
    8: "여덟",
    9: "아홉",
    10: "열",
    20: "스무",
}
NATIVE_TENS = {
    20: "스물",
    30: "서른",
    40: "마흔",
    50: "쉰",
    60: "예순",
    70: "일흔",
    80: "여든",
    90: "아흔",
}

NATIVE_COUNTERS = (
    "개",
    "명",
    "분",
    "번",
    "살",
    "마리",
    "권",
    "장",
    "대",
    "채",
    "켤레",
    "그루",
    "송이",
    "자루",
    "통",
    "잔",
    "병",
    "시",
    "가지",
    "달",
)
"""고유어로 읽는 단위. **'분'은 함정이다** — 사람을 셀 때는 고유어(세 분),
시간을 셀 때는 한자어(삼십 분)다. 아래에서 문맥으로 가른다."""

SINO_COUNTERS = (
    "원",
    "년",
    "월",
    "일",
    "초",
    "％",
    "%",
    "퍼센트",
    "도",
    "층",
    "호",
    "회",
    "km",
    "kg",
    "m",
    "cm",
    "g",
    "ml",
    "l",
    "일간",
    "개월",
    "주",
)

UNIT_READING = {
    "%": "퍼센트",
    "％": "퍼센트",
    "km": "킬로미터",
    "kg": "킬로그램",
    "cm": "센티미터",
    "ml": "밀리리터",
    "m": "미터",
    "g": "그램",
    "l": "리터",
    "℃": "도",
    "$": "달러",
}

_ALPHABET = {
    "A": "에이",
    "B": "비",
    "C": "씨",
    "D": "디",
    "E": "이",
    "F": "에프",
    "G": "지",
    "H": "에이치",
    "I": "아이",
    "J": "제이",
    "K": "케이",
    "L": "엘",
    "M": "엠",
    "N": "엔",
    "O": "오",
    "P": "피",
    "Q": "큐",
    "R": "알",
    "S": "에스",
    "T": "티",
    "U": "유",
    "V": "브이",
    "W": "더블유",
    "X": "엑스",
    "Y": "와이",
    "Z": "지",
}


def sino(number: int) -> str:
    """한자어 수사. 금액·날짜·일반 수에 쓴다."""
    if number == 0:
        return "영"
    if number < 0:
        return "마이너스 " + sino(-number)

    groups: list[str] = []
    index = 0
    while number > 0:
        chunk = number % 10000
        if chunk:
            reading = _sino_group(chunk) + SINO_BIG_UNITS[index]
            # 최상위 자리의 '일만'은 '만'으로 읽는다. "일만 원"이라 말하는
            # 사람은 없다(수표에나 쓴다). 중간 자리의 '일'은 남긴다 — "일억 일만".
            if chunk == 1 and index > 0 and number // 10000 == 0:
                reading = SINO_BIG_UNITS[index]
            groups.append(reading)
        number //= 10000
        index += 1
    # 만 단위로 띄운다. 사람도 "백이십삼만 / 사천오백육십칠"로 끊어 읽고,
    # 붙여 두면 TTS가 한 호흡에 몰아쳐 숫자를 알아들을 수 없다.
    return " ".join(reversed(groups))


def _sino_group(chunk: int) -> str:
    out: list[str] = []
    for position in range(3, -1, -1):
        digit = (chunk // 10**position) % 10
        if digit == 0:
            continue
        # 십·백·천 앞의 '일'은 읽지 않는다. "일십오"가 아니라 "십오"다.
        if digit == 1 and position > 0:
            out.append(SINO_SMALL_UNITS[position])
        else:
            out.append(SINO_DIGITS[digit] + SINO_SMALL_UNITS[position])
    return "".join(out)


def native(number: int) -> str:
    """고유어 수사(관형형). 개수를 셀 때 쓴다.

    100 이상은 고유어로 세지 않는다 — "백스물세 개"라고 말하는 사람은 없다.
    """
    if not 1 <= number < 100:
        return sino(number)
    if number in NATIVE_ATTRIBUTIVE:
        return NATIVE_ATTRIBUTIVE[number]

    tens, ones = divmod(number, 10)
    if tens == 0:
        return NATIVE_ATTRIBUTIVE[ones]
    if ones == 0:
        return "스무" if tens == 2 else NATIVE_TENS[tens * 10]
    prefix = "열" if tens == 1 else NATIVE_TENS[tens * 10]
    return prefix + NATIVE_ATTRIBUTIVE[ones]


# --- 패턴 ---------------------------------------------------------------
#
# 순서가 중요하다. 전화번호를 먼저 잡지 않으면 일반 숫자 규칙이 조각내 버린다.

_PHONE = re.compile(r"\b(0\d{1,2})[-.](\d{3,4})[-.](\d{4})\b")
_RRN = re.compile(r"\b(\d{6})-(\d{7})\b")
_DATE = re.compile(r"\b(\d{4})[-./](\d{1,2})[-./](\d{1,2})\b")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_MIXED_MAGNITUDE = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(조|억|만|천|백|십)")
"""``1억 5천만원`` 처럼 숫자와 한글 자릿수가 섞인 표기. 금융 상담에서 흔하다.
단위 규칙이 먼저 잡으면 '억'을 단위로 보고 "일 억"이라 끊어 읽는다."""

_MONEY = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+)\s*원")
_NUMBER_UNIT = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(%|％|[A-Za-z]{1,2}|[가-힣]{1,3})")
_BARE_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b")
_ENGLISH_WORD = re.compile(r"\b[A-Z]{2,}\b")


def _digits(text: str) -> int:
    return int(text.replace(",", ""))


def _read_digits_one_by_one(text: str) -> str:
    """전화번호처럼 한 자리씩 읽는다. 0은 '공'이다 — '영'이라 읽으면 어색하다."""
    return "".join("공" if ch == "0" else SINO_DIGITS[int(ch)] for ch in text if ch.isdigit())


PEOPLE_HINTS = ("고객", "손님", "참석", "직원", "가족", "동반", "인원")
"""'분'을 사람으로 읽을 단서. 상담에서 '분'은 대부분 시간(삼십 분)이라
한자어를 기본으로 두고, 이 단서가 가까이 있을 때만 고유어로 읽는다.

완전하지 않다. "다섯 분 오셨어요"를 "오 분"으로 읽을 수 있고, 그건 어색하다.
다만 반대 실수("삼십 분 걸립니다"를 "서른 분")가 훨씬 자주 나고 더 이상하다."""


def _counter_reading(value: int, unit: str, context: str) -> str:
    """단위에 맞는 수사를 고른다.

    고유어(세 개)와 한자어(삼 분)를 가르는 것이 한국어 TTS의 핵심이다.
    "삼 개"나 "서른 분 걸립니다"는 사람이 듣자마자 기계라는 것을 안다.
    """
    if unit in UNIT_READING:
        return f"{sino(value)} {UNIT_READING[unit]}"
    if unit == "분":
        people = any(hint in context for hint in PEOPLE_HINTS)
        return f"{native(value)} 분" if people else f"{sino(value)} 분"
    if unit in NATIVE_COUNTERS:
        return f"{native(value)} {unit}"
    return f"{sino(value)} {unit}"


def _split_counter(token: str) -> tuple[str, str]:
    """``개를`` → ``("개", "를")``. 조사가 붙어도 단위를 알아본다.

    조사를 단위의 일부로 보면 "3개를"이 한자어로 읽혀 "삼 개를"이 된다.
    """
    known = sorted(set(NATIVE_COUNTERS) | set(SINO_COUNTERS), key=len, reverse=True)
    for counter in known:
        if token.startswith(counter):
            return counter, token[len(counter) :]
    return token, ""


def normalize(text: str) -> str:
    """TTS에 넣기 전 텍스트를 읽는 대로 바꾼다."""
    if not text:
        return text

    # 개인정보는 한 자리씩. 붙여 읽으면 고객이 받아 적지 못한다.
    text = _RRN.sub(
        lambda m: f"{_read_digits_one_by_one(m[1])} {_read_digits_one_by_one(m[2])}", text
    )
    text = _PHONE.sub(lambda m: " ".join(_read_digits_one_by_one(g) for g in m.groups()), text)

    text = _DATE.sub(lambda m: f"{sino(int(m[1]))}년 {sino(int(m[2]))}월 {sino(int(m[3]))}일", text)
    # 시각은 시(고유어) + 분(한자어)가 섞인다. 한국어 TTS의 대표적 오답 지점이다.
    text = _TIME.sub(
        lambda m: f"{native(int(m[1]))} 시" + (f" {sino(int(m[2]))} 분" if int(m[2]) else " 정각"),
        text,
    )
    # 숫자+한글 자릿수를 먼저 붙인다. 나중에 처리하면 '억'이 단위로 잡혀
    # "일 억 오 천만원"처럼 끊어진다.
    text = _MIXED_MAGNITUDE.sub(lambda m: f"{sino(_digits(m[1]))}{m[2]}", text)

    # 금액을 단위 규칙보다 먼저 처리한다. "50,000원"이 "오영영영영 원"이 되면
    # 고객은 금액을 알아듣지 못하고, 그 통화는 실패한 통화다.
    text = _MONEY.sub(lambda m: f"{sino(_digits(m[1]))} 원", text)

    def _unit_sub(match: re.Match[str]) -> str:
        unit, tail = _split_counter(match[2])
        # 앞뒤를 함께 본다. '분'이 시간인지 사람인지는 주변 낱말로만 알 수 있다.
        window = text[max(0, match.start() - 12) : match.end() + 12]
        return _counter_reading(_digits(match[1]), unit, window) + tail

    text = _NUMBER_UNIT.sub(_unit_sub, text)
    text = _BARE_NUMBER.sub(lambda m: sino(_digits(m[0])), text)

    # 대문자 약어는 철자로 읽는다. "VIP"를 단어로 읽으려는 엔진이 많다.
    text = _ENGLISH_WORD.sub(lambda m: "".join(_ALPHABET.get(c, c) for c in m[0]), text)

    return re.sub(r"\s{2,}", " ", text).strip()
