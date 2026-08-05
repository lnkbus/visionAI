#!/usr/bin/env bash
# 온프렘 릴리스 빌드 — 공급사 빌드 서버에서 실행한다.
#
# 개발 체크아웃과 릴리스 빌드의 차이는 딱 하나, **공개키가 내장돼 있는가**다.
# 그 하나가 빠지면 서명 검증이 꺼진 채로 나가는데, 그 상태는 겉으로 완전히
# 정상으로 보인다 — 블록은 뜨고 상담은 돌고 아무 오류도 없다.
# 그래서 사람이 눈으로 확인할 수 없고, 마지막에 기계가 막는다.
#
#   ./build_release.sh --public-key /secure/keys/license_pub.pem --out dist/release
#   ./build_release.sh --public-key ... --out ... --obfuscate   # PyArmor 사용 시
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PUBLIC_KEY=""
OUT=""
OBFUSCATE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-key) PUBLIC_KEY="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --obfuscate) OBFUSCATE=1; shift ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$PUBLIC_KEY" ]] || { echo "--public-key 가 필요하다" >&2; exit 1; }
[[ -n "$OUT" ]] || { echo "--out 이 필요하다" >&2; exit 1; }

echo "▸ 소스 복제"
rm -rf "$OUT"; mkdir -p "$OUT"
# git archive는 추적되는 파일만 담는다. 로컬에 굴러다니는 키·.env·산출물이
# 딸려 나가는 것을 원천적으로 막는다 — cp -r 이었다면 반드시 뭔가 새어 나간다.
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$OUT"

echo "▸ 공급사 전용 자산 제거"
# 발급기가 함께 나가면 개인키만 구하면 라이선스를 찍어낼 수 있는 상태가 된다.
rm -rf "$OUT/tools/licgen"
# 테스트에는 고정 비밀키·샘플 라이선스가 들어 있다. 유출은 아니지만
# 반입 검토 대상만 늘리고 고객사에 혼란을 준다.
find "$OUT" -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$OUT/.github" "$OUT/docs" "$OUT/.importlinter"

echo "▸ 공개키 내장 — 이 단계를 거쳐야 서명 검증이 켜진다"
(cd "$REPO_ROOT" && uv run blockctl embed-key --public-key "$PUBLIC_KEY" --root "$OUT")

if [[ "$OBFUSCATE" -eq 1 ]]; then
  echo "▸ 난독화 (PyArmor)"
  if ! command -v pyarmor >/dev/null 2>&1; then
    echo "  ✗ pyarmor가 없다. 라이선스가 필요한 상용 도구다 — 빌드 서버에 설치한다." >&2
    exit 1
  fi
  # 블록별로 돌린다. 한 번에 트리 전체를 넘기면 어느 모듈에서 실패했는지
  # 알 수 없고, 폐쇄망 반입 직전에 원인을 찾기 어렵다.
  for pkg in "$OUT"/blocks/*/src/vai_* "$OUT"/libs/*/src/vai_*; do
    [[ -d "$pkg" ]] || continue
    echo "  - $(basename "$pkg")"
    pyarmor gen --output "$(dirname "$pkg")" --recursive "$pkg"
  done
else
  echo "▸ 난독화 생략 (--obfuscate 로 활성화)"
  echo "  소스가 그대로 나간다. 계약상 소스 보호가 필요한 사업에서는 반드시 켠다."
fi

echo "▸ 릴리스 검사"
# 사람이 눈으로 확인할 수 없는 것들만 모았다.
# 여기서 막지 못하면 고객사 현장에서야 드러나고, 개인키 유출은 되돌릴 수 없다.
(cd "$REPO_ROOT" && uv run blockctl release-check --root "$OUT")

VERSION="$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)"
cat > "$OUT/RELEASE" <<EOF
version=$VERSION
commit=$(git -C "$REPO_ROOT" rev-parse HEAD)
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
obfuscated=$OBFUSCATE
public_key_sha256=$(sha256sum "$PUBLIC_KEY" | cut -d' ' -f1)
EOF

echo
echo "✓ 릴리스 트리: $OUT"
echo "  공개키 지문: $(sha256sum "$PUBLIC_KEY" | cut -c1-16)…"
echo "  다음: deploy/airgap/build_bundle.sh 로 반입 번들을 만든다"
