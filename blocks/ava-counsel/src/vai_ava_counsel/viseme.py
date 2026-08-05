"""한글 → 입 모양(viseme) 변환.

한글은 음절 하나가 초성·중성·종성으로 분해되고, 그 구조가 유니코드 코드포인트에
그대로 들어 있다. 그래서 발음 사전 없이도 입 모양을 꽤 정확히 뽑을 수 있다 —
영어라면 발음 사전이 필요한 일이다.

**양순음(ㅁ·ㅂ·ㅍ)의 입술 다물기가 가장 중요하다.** 사람은 다른 입 모양의
오차는 잘 못 느끼지만 "마"에서 입이 안 닫히면 즉시 가짜라고 안다. 그래서
자음 중에서도 양순음에 가장 긴 시간을 준다.

**타이밍은 실측 기반이다.** TTS가 합성한 조각의 실제 길이를 받아 그 안에
음절을 나눠 넣는다. 글자 수로 추정하면 어절마다 조금씩 어긋나고, 문장이
길어질수록 누적돼 끝에서는 눈에 띄게 벌어진다.
"""

from __future__ import annotations

from vai_contracts.avatar import Viseme, VisemeFrame

HANGUL_BASE = 0xAC00
HANGUL_LAST = 0xD7A3
JUNG_COUNT = 21
JONG_COUNT = 28

CHOSEONG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNGSEONG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONGSEONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"

# 자음 → 입 모양. 시각적으로 구분되는 것만 남긴다.
CONSONANT_VISEME: dict[str, Viseme] = {
    "ㅁ": Viseme.PP,
    "ㅂ": Viseme.PP,
    "ㅃ": Viseme.PP,
    "ㅍ": Viseme.PP,
    "ㄴ": Viseme.DD,
    "ㄷ": Viseme.DD,
    "ㄸ": Viseme.DD,
    "ㅌ": Viseme.DD,
    "ㄹ": Viseme.DD,
    "ㅅ": Viseme.CH,
    "ㅆ": Viseme.CH,
    "ㅈ": Viseme.CH,
    "ㅉ": Viseme.CH,
    "ㅊ": Viseme.CH,
    "ㄱ": Viseme.KK,
    "ㄲ": Viseme.KK,
    "ㅋ": Viseme.KK,
    "ㅎ": Viseme.AA,
    # ㅇ은 초성에서 소리가 없다. 입 모양을 넣으면 없는 자음이 보인다.
}

# 모음 → 입 모양. 이중모음은 끝소리를 따른다 — 입은 결국 그 모양으로 간다.
VOWEL_VISEME: dict[str, Viseme] = {
    "ㅏ": Viseme.AA,
    "ㅑ": Viseme.AA,
    "ㅐ": Viseme.E,
    "ㅒ": Viseme.E,
    "ㅔ": Viseme.E,
    "ㅖ": Viseme.E,
    "ㅓ": Viseme.OH,
    "ㅕ": Viseme.OH,
    "ㅗ": Viseme.OH,
    "ㅛ": Viseme.OH,
    "ㅜ": Viseme.OU,
    "ㅠ": Viseme.OU,
    "ㅟ": Viseme.OU,
    "ㅚ": Viseme.OU,
    "ㅡ": Viseme.IH,
    "ㅣ": Viseme.IH,
    "ㅢ": Viseme.IH,
    "ㅘ": Viseme.AA,
    "ㅙ": Viseme.E,
    "ㅝ": Viseme.OH,
    "ㅞ": Viseme.E,
}

BILABIAL_RATIO = 0.45
"""양순음이 음절에서 차지하는 비율. 다른 자음(0.3)보다 길게 준다 —
입술이 닫히는 순간이 짧으면 눈에 안 보이고, 그러면 "마"가 "아"로 보인다."""

CONSONANT_RATIO = 0.3
CODA_RATIO = 0.15
"""받침. 짧지만 있어야 한다 — 없으면 "밥"이 "바"처럼 보인다."""

MIN_FRAME_MS = 20
"""이보다 짧은 프레임은 만들지 않는다. 60fps에서도 한 프레임 남짓이라
그려지지 않고, 개수만 늘려 전송량을 키운다."""


def decompose(syllable: str) -> tuple[str, str, str] | None:
    """한글 음절을 초성·중성·종성으로 나눈다. 한글이 아니면 ``None``."""
    code = ord(syllable)
    if not HANGUL_BASE <= code <= HANGUL_LAST:
        return None
    offset = code - HANGUL_BASE
    cho = offset // (JUNG_COUNT * JONG_COUNT)
    jung = (offset % (JUNG_COUNT * JONG_COUNT)) // JONG_COUNT
    jong = offset % JONG_COUNT
    return CHOSEONG[cho], JUNGSEONG[jung], JONGSEONG[jong].strip()


def syllable_visemes(syllable: str) -> list[tuple[Viseme, float]]:
    """음절 하나를 ``(입 모양, 비율)`` 목록으로. 비율의 합은 1이다."""
    parts = decompose(syllable)
    if parts is None:
        # 숫자·기호·영문. 전처리를 거치면 대부분 한글이 되지만, 남은 것은
        # 중립적으로 살짝 벌린 입으로 처리한다 — 무음보다 자연스럽다.
        return [(Viseme.E, 1.0)] if syllable.strip() else [(Viseme.SIL, 1.0)]

    cho, jung, jong = parts
    out: list[tuple[Viseme, float]] = []

    onset = CONSONANT_VISEME.get(cho)
    if onset is not None:
        ratio = BILABIAL_RATIO if onset is Viseme.PP else CONSONANT_RATIO
        out.append((onset, ratio))

    coda = CONSONANT_VISEME.get(jong) if jong else None
    coda_ratio = CODA_RATIO if coda is not None else 0.0

    vowel = VOWEL_VISEME.get(jung, Viseme.AA)
    used = sum(r for _, r in out) + coda_ratio
    out.append((vowel, max(0.1, 1.0 - used)))
    if coda is not None:
        out.append((coda, coda_ratio))
    return out


def build_track(text: str, duration_ms: int) -> list[VisemeFrame]:
    """텍스트와 **실측 길이**로 입 모양 타임라인을 만든다.

    글자 수로 시간을 추정하지 않는다. 어절마다 조금씩 어긋나고, 문장이 길어질수록
    누적돼 끝에서는 눈에 띄게 벌어진다. TTS가 합성한 실제 길이를 쓴다.
    """
    if duration_ms <= 0:
        return []

    syllables = [ch for ch in text if not ch.isspace()]
    if not syllables:
        return [VisemeFrame(at_ms=0, viseme=Viseme.SIL)]

    per_syllable = duration_ms / len(syllables)
    frames: list[VisemeFrame] = []
    cursor = 0.0

    for syllable in syllables:
        for viseme, ratio in syllable_visemes(syllable):
            span = per_syllable * ratio
            at_ms = int(cursor)
            # 같은 모양이 이어지면 프레임을 합친다. 나눠 보내도 화면은 똑같고
            # 전송량만 는다 — 통화당 수백 프레임이 쌓인다.
            if frames and frames[-1].viseme is viseme:
                cursor += span
                continue
            if frames and at_ms - frames[-1].at_ms < MIN_FRAME_MS:
                cursor += span
                continue
            frames.append(VisemeFrame(at_ms=at_ms, viseme=viseme))
            cursor += span

    # 끝에 입을 다문다. 없으면 마지막 모양이 화면에 남아 아바타가 입을 벌린 채
    # 굳어 있는 것처럼 보인다.
    if frames[-1].viseme is not Viseme.SIL:
        frames.append(VisemeFrame(at_ms=duration_ms, viseme=Viseme.SIL))
    return frames
