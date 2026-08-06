"""광고한 모델을 실제로 받을 수 있는가.

`fetch_models.sh` 는 이름에서 저장소를 **규칙으로 만들었다**:

    repo = f"Systran/faster-whisper-{model}"

`--list` 와 문서는 `large-v3-turbo` 를 권장했는데, 그 이름의 Systran 저장소는
없다. 그래서 반입 담당자가 받은 것은 이것이다:

    RepositoryNotFoundError: 401 Client Error ... /Systran/faster-whisper-large-v3-turbo

401 은 **권한 문제처럼 읽힌다.** 토큰을 찾고 프록시를 의심하다 시간을 버리는데,
실제로는 우리가 없는 이름을 만들어 낸 것이었다. 규칙이 조용히 틀리는 자리다.

그래서 저장소를 표로 적고, 이 시험이 두 가지를 지킨다.

1. 규칙으로 돌아가지 않는다.
2. **문서가 권하는 이름은 표에 있다.** 표와 문서가 갈라지면 없는 모델을
   광고하게 되고, 그 사실은 폐쇄망 현장에서야 드러난다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "airgap" / "fetch_models.sh"
DOCS = sorted((ROOT / "docs").glob("*.md"))


def _catalog() -> dict[str, tuple[str, str, str]]:
    """이름 → (저장소, 크기, 쓸 자리)."""
    text = SCRIPT.read_text("utf-8")
    block = re.search(r"^STT_MODELS='\n(.*?)^'", text, re.MULTILINE | re.DOTALL)
    assert block, "STT_MODELS 표를 못 찾았다 — 아래 대조가 전부 무의미해진다"

    found: dict[str, tuple[str, str, str]] = {}
    for line in block.group(1).splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        assert len(fields) == 4, f"칸이 넷이 아니다: {line!r}"
        name, repo, size, note = (f.strip() for f in fields)
        assert name not in found, f"이름이 겹친다: {name}"
        found[name] = (repo, size, note)
    return found


def test_표를_찾았다() -> None:
    catalog = _catalog()

    assert len(catalog) >= 4, f"모델이 너무 적다 — 파싱이 낡았다 ({sorted(catalog)})"
    assert "small" in catalog, "기본값이 표에 없다"


@pytest.mark.parametrize("name", sorted(_catalog()))
def test_모든_이름에_저장소가_적혀_있다(name: str) -> None:
    repo, size, note = _catalog()[name]

    assert repo.count("/") == 1, f"{name}: 저장소가 소유자/이름 꼴이 아니다 ({repo!r})"
    assert size and note, f"{name}: 크기·쓸 자리를 비워 두면 반입 매체를 못 고른다"


def test_이름에서_저장소를_만들어_내지_않는다() -> None:
    """규칙은 Systran 이 올린 것에만 맞았다. 없는 이름을 만들어 401 을 받는다."""
    text = SCRIPT.read_text("utf-8")

    assert 'f"Systran/faster-whisper-{' not in text, (
        "저장소를 이름에서 만들고 있다 — STT_MODELS 표에서 찾아 쓴다"
    )


def test_전부_같은_소유자가_아니다() -> None:
    """표를 적어 둔 이유 자체다. turbo 는 Systran 이 아니라 제3자 변환본이고,
    반입 심사에서 출처를 묻는다. 전부 한 소유자로 보이면 표가 규칙으로
    되돌아갔다는 뜻이다."""
    owners = {repo.split("/")[0] for repo, _, _ in _catalog().values()}

    assert len(owners) > 1, f"소유자가 하나뿐이다 ({owners}) — 규칙으로 되돌아갔는지 본다"


def _advertised_names() -> dict[str, list[str]]:
    """문서가 권하는 모델 이름 → 그 이름이 나온 문서들."""
    seen: dict[str, list[str]] = {}
    patterns = (
        re.compile(r"--stt\s+([\w.-]+)"),
        re.compile(r"faster-whisper-([\w.-]+)"),
    )
    for doc in DOCS:
        text = doc.read_text("utf-8")
        for pattern in patterns:
            for name in pattern.findall(text):
                seen.setdefault(name, []).append(doc.name)
    return seen


def test_문서에서_모델_이름을_찾았다() -> None:
    """문서 형식이 바뀌어 하나도 못 찾으면 아래 대조가 조용히 통과한다."""
    assert _advertised_names(), "문서에서 모델 이름을 하나도 못 찾았다 — 정규식이 낡았다"


@pytest.mark.parametrize("name", sorted(_advertised_names()))
def test_문서가_권하는_이름은_받을_수_있다(name: str) -> None:
    catalog = _catalog()

    assert name in catalog, (
        f"문서({', '.join(sorted(set(_advertised_names()[name])))})가 "
        f'"{name}" 을 권하는데 fetch_models.sh 로는 못 받는다. '
        f"표에 넣거나 문서를 고친다. 표에 있는 이름: {sorted(catalog)}"
    )
