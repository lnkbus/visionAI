#!/usr/bin/env bash
# 에어갭 반입 번들 생성 — 공급사 빌드 서버에서 실행한다.
#
# 폐쇄망 반입은 되돌릴 수 없다. USB로 들고 들어간 뒤 "이미지 하나가 빠졌다"를
# 알게 되면 반출입 승인을 다시 밟아야 하고, 고객사에 따라 며칠이 걸린다.
# 그래서 담을 목록을 사람이 관리하지 않고 라이선스와 카탈로그에서 계산한다.
#
#   ./build_bundle.sh --license customer.lic --out dist/
#   ./build_bundle.sh --block TA-ASSIST --block UI-AGENT --out dist/
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=portable.sh
. "$(dirname "${BASH_SOURCE[0]}")/portable.sh"
OUT_DIR=""
LICENSE_FILE=""
BLOCKS=()
TAG="0.1.0"
SKIP_BUILD=0

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --license) LICENSE_FILE="$2"; shift 2 ;;
    --block) BLOCKS+=("$2"); shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;   # 이미 빌드된 이미지를 담을 때
    -h|--help) usage 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; usage 1 ;;
  esac
done

[[ -n "$OUT_DIR" ]] || { echo "--out 이 필요하다" >&2; exit 1; }
if [[ -z "$LICENSE_FILE" && ${#BLOCKS[@]} -eq 0 ]]; then
  echo "--license 또는 --block 이 필요하다" >&2; exit 1
fi

STAGE="$OUT_DIR/visionai-$TAG"
rm -rf "$STAGE"; mkdir -p "$STAGE"

echo "▸ 반입 계획 계산"
PLAN_ARGS=(--root "$REPO_ROOT" --out "$STAGE/plan.json")
[[ -n "$LICENSE_FILE" ]] && PLAN_ARGS+=(--license "$LICENSE_FILE")
for b in "${BLOCKS[@]+"${BLOCKS[@]}"}"; do PLAN_ARGS+=(--block "$b"); done
(cd "$REPO_ROOT" && uv run blockctl bundle-plan "${PLAN_ARGS[@]}")

read_into IMAGES < <(python3 -c '
import json, sys
print("\n".join(json.load(open(sys.argv[1]))["images"]))' "$STAGE/plan.json")
read_into PLAN_BLOCKS < <(python3 -c '
import json, sys
print("\n".join(json.load(open(sys.argv[1]))["blocks"]))' "$STAGE/plan.json")

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  echo "▸ 블록 이미지 빌드 (${#PLAN_BLOCKS[@]}개)"
  for block_id in "${PLAN_BLOCKS[@]}"; do
    dir="$(echo "$block_id" | tr '[:upper:]' '[:lower:]')"
    echo "  - $dir"
    docker build -q -f "$REPO_ROOT/deploy/docker/Dockerfile" \
      --build-arg "BLOCK=$dir" -t "visionai/$dir:$TAG" "$REPO_ROOT" >/dev/null
  done
fi

echo "▸ 인프라 이미지 확보"
for image in "${IMAGES[@]}"; do
  [[ "$image" == visionai/* ]] && continue
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
done

# 한 번에 save 하면 공통 레이어가 중복 저장되지 않는다. 블록마다 따로 저장하면
# 베이스 이미지가 블록 수만큼 복사되어 번들이 수 GB 커진다.
echo "▸ 이미지 저장 (단일 아카이브 — 레이어 중복 제거)"
docker save "${IMAGES[@]}" -o "$STAGE/images.tar"

echo "▸ 설치 자산 복사"
cp "$REPO_ROOT/deploy/airgap/install.sh" "$REPO_ROOT/deploy/airgap/selftest.sh" "$STAGE/"
# 설치 마법사. 표준 라이브러리만 쓰므로 반입 대상이 늘지 않는다.
cp -r "$REPO_ROOT/deploy/airgap/installer" "$STAGE/"
# 이식 계층. install.sh·selftest.sh 가 source 하므로 함께 들어가야 한다.
cp "$REPO_ROOT/deploy/airgap/portable.sh" "$STAGE/"
cp "$REPO_ROOT/deploy/compose/docker-compose.yml" "$STAGE/"
cp "$REPO_ROOT/deploy/compose/compliance-rules.json" "$STAGE/" 2>/dev/null || true
chmod +x "$STAGE/install.sh" "$STAGE/selftest.sh"
printf '%s\n' "$TAG" > "$STAGE/VERSION"

# 반입 매체는 손상되거나 바꿔치기될 수 있다. 설치 전에 대조하지 않으면
# 반쯤 깨진 이미지를 로드하고 원인 모를 장애를 쫓게 된다.
echo "▸ 체크섬"
(cd "$STAGE" && sorted_files . | sha256_list > SHA256SUMS)

echo "▸ 아카이브"
tar -C "$OUT_DIR" -czf "$OUT_DIR/visionai-$TAG.tar.gz" "visionai-$TAG"
echo "$(sha256_of "$OUT_DIR/visionai-$TAG.tar.gz")  visionai-$TAG.tar.gz" \
  > "$OUT_DIR/visionai-$TAG.tar.gz.sha256"

echo
echo "✓ 번들: $OUT_DIR/visionai-$TAG.tar.gz ($(du -h "$OUT_DIR/visionai-$TAG.tar.gz" | cut -f1))"
echo "  블록 ${#PLAN_BLOCKS[@]}개 / 이미지 ${#IMAGES[@]}개"
echo "  설치: sudo ./install.sh  또는  ./install.sh --gui (브라우저 마법사)"
echo "  반출 시 .sha256 파일을 **별도 경로**로 전달한다 — 같은 매체에 두면"
echo "  매체가 바꿔치기될 때 체크섬도 함께 바뀐다."
