#!/usr/bin/env bash
# 폐쇄망 설치기 — 고객사 서버에서 실행한다.
#
# 설치 중 실패는 흔하다. 문제는 **어디까지 진행됐는지 모르는 실패**다.
# 그래서 순서를 고정한다: 사전 점검 → 무결성 대조 → 적재 → 기동 → 자가진단.
# 앞 단계가 실패하면 뒤 단계를 시작하지 않는다.
#
#   sudo ./install.sh                # 설치
#   ./install.sh --dry-run           # 반입 전 점검만 (docker 조작 없음)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
DATA_DIR="/var/lib/visionai"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

fail() { echo "  ✗ $*" >&2; FAILED=1; }
ok()   { echo "  ✓ $*"; }
FAILED=0

echo "── 1/5 사전 점검"

if [[ "$DRY_RUN" -eq 0 ]]; then
  command -v docker >/dev/null 2>&1 && ok "docker $(docker --version | awk '{print $3}' | tr -d ,)" \
    || fail "docker가 없다"
  docker compose version >/dev/null 2>&1 && ok "docker compose" \
    || fail "docker compose 플러그인이 없다"
else
  command -v docker >/dev/null 2>&1 && ok "docker" || echo "  · docker 없음 (dry-run이라 계속)"
fi

# 이미지 적재는 압축을 풀어 저장하므로 아카이브보다 훨씬 많은 공간을 쓴다.
# 여유 없이 시작했다가 중간에 멈추면 절반만 적재된 상태가 남는다.
if [[ -f "$HERE/images.tar" ]]; then
  NEED_MB=$(( $(stat -c%s "$HERE/images.tar") / 1048576 * 3 ))
  AVAIL_MB=$(df -Pm "$HERE" | awk 'NR==2 {print $4}')
  if (( AVAIL_MB > NEED_MB )); then
    ok "디스크 여유 ${AVAIL_MB}MB (필요 추정 ${NEED_MB}MB)"
  else
    fail "디스크 부족: ${AVAIL_MB}MB 있음 / 약 ${NEED_MB}MB 필요"
  fi
else
  fail "images.tar 가 없다 — 번들이 온전하지 않다"
fi

# 포트가 이미 쓰이고 있으면 기동은 성공하고 트래픽만 엉뚱한 곳으로 간다.
#
# 단, **재설치(업그레이드)에서는 우리 자신이 그 포트를 쓰고 있다.** 그것까지
# 충돌로 막으면 업그레이드 경로가 통째로 닫힌다 — 폐쇄망에서 업그레이드는
# 재설치가 유일한 수단이므로 치명적이다.
UPGRADE=0
if [[ -f "$HERE/docker-compose.yml" ]] && command -v docker >/dev/null 2>&1; then
  if [[ -n "$(cd "$HERE" && docker compose -f docker-compose.yml ps -q 2>/dev/null)" ]]; then
    UPGRADE=1
  fi
fi

BUSY=()
for port in 8080 8081 8095 8096 8097 6379 6333; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then BUSY+=("$port"); exec 3<&- 3>&-; fi
done
if [[ ${#BUSY[@]} -eq 0 ]]; then
  ok "포트 충돌 없음"
elif [[ "$UPGRADE" -eq 1 ]]; then
  ok "기존 설치 감지 — 업그레이드로 진행 (사용 중 포트: ${BUSY[*]})"
else
  fail "이미 쓰이는 포트: ${BUSY[*]} (다른 서비스가 쓰고 있다면 먼저 정리한다)"
fi

for name in plan.json docker-compose.yml selftest.sh SHA256SUMS; do
  [[ -f "$HERE/$name" ]] && ok "$name" || fail "$name 이 없다"
done

echo
echo "── 2/5 무결성 대조"
# 반입 매체는 손상되거나 바꿔치기될 수 있다. 대조 없이 적재하면 반쯤 깨진
# 이미지를 로드하고 원인 모를 장애를 쫓게 된다.
if [[ -f "$HERE/SHA256SUMS" ]]; then
  if (cd "$HERE" && sha256sum --quiet -c SHA256SUMS); then
    ok "$(wc -l < "$HERE/SHA256SUMS")개 파일 일치"
  else
    fail "체크섬 불일치 — 이 번들로 설치하면 안 된다"
  fi
fi

if [[ "$FAILED" -ne 0 ]]; then
  echo
  echo "✗ 사전 점검 실패 — 설치를 진행하지 않는다." >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "✓ 점검 통과 (dry-run — 적재·기동은 하지 않았다)"
  exit 0
fi

echo
echo "── 3/5 이미지 적재"
docker load -i "$HERE/images.tar"

echo
echo "── 4/5 기동"
mkdir -p "$DATA_DIR/audit" "$DATA_DIR/licenses"
chmod 700 "$DATA_DIR/audit"
(cd "$HERE" && docker compose -f docker-compose.yml up -d)

echo
echo "── 5/5 자가진단"
"$HERE/selftest.sh"

cat <<'NEXT'

다음 단계 — 라이선스 설치 (아직 설치되지 않았다):

  1) 이 서버의 발급요청서를 만든다
     curl -sS -X POST localhost:8095/internal/v1/license/request \
       -H 'content-type: application/json' \
       -d '{"customer_id":"<고객사>","site":"<사이트>"}' -o license.req

  2) license.req 를 반출해 공급사에 전달한다

  3) 받은 .lic 을 반입해 설치한다
     curl -sS -X POST localhost:8095/internal/v1/license \
       -H 'content-type: application/json' \
       -d "{\"content\": $(cat customer.lic), \"activate\": true}"

  4) 운영 콘솔에서 확인:  http://<서버>:8097/console
NEXT
