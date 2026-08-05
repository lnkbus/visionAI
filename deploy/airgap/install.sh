#!/usr/bin/env bash
# 폐쇄망 설치기 — 고객사 서버에서 실행한다.
#
# 설치 중 실패는 흔하다. 문제는 **어디까지 진행됐는지 모르는 실패**다.
# 그래서 순서를 고정한다: 사전 점검 → 무결성 대조 → 적재 → 기동 → 자가진단.
# 앞 단계가 실패하면 뒤 단계를 시작하지 않는다.
#
#   sudo ./install.sh                # 설치 (전체)
#   ./install.sh --gui               # 브라우저 설치 마법사
#   ./install.sh --dry-run           # 반입 전 점검만 (docker 조작 없음)
#   sudo ./install.sh --blocks "STT-CORE TA-ASSIST"   # 일부만 기동
#
# 리눅스와 macOS에서 돈다(bash 3.2 이상). Windows는 WSL2 안에서 실행한다 —
# 컨테이너 이미지가 리눅스용이라 네이티브 지원은 사실상 다른 제품이 된다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# GNU coreutils가 없는 곳(macOS)에서도 같은 답을 내게 한다.
# shellcheck source=portable.sh
. "$HERE/portable.sh"

DRY_RUN=0
DATA_DIR="/var/lib/visionai"
BLOCKS=""
GUI=0
GUI_PORT=8099

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --blocks) BLOCKS="$2"; shift 2 ;;
    --gui) GUI=1; shift ;;
    --gui-port) GUI_PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

# ── 설치 마법사 ─────────────────────────────────────────────────────────────
# 화면은 이 스크립트를 대체하지 않고 **몰아준다**. 설치 단계를 두 번 구현하면
# 둘이 갈라지고, 갈라진 사실은 현장에서 한쪽만 실패할 때 드러난다.
if [[ "$GUI" -eq 1 ]]; then
  PY="$(command -v python3 || true)"
  if [[ -z "$PY" ]]; then
    echo "✗ python3 가 없다 — 화면 없이 설치한다: sudo ./install.sh" >&2
    exit 1
  fi
  exec "$PY" "$HERE/installer/server.py" --bundle "$HERE" --port "$GUI_PORT"
fi

# 진행 상황을 기계가 읽을 수 있게 함께 내보낸다. VAI_PROGRESS=1 일 때만 나오므로
# 사람이 보는 출력은 그대로다. 화면이 산문을 파싱하게 두면 문구를 고칠 때마다
# 화면이 조용히 깨진다.
mark() { [[ "${VAI_PROGRESS:-0}" == "1" ]] && echo "@@$1|$2" || true; }

fail() { echo "  ✗ $*" >&2; mark FAIL "$*"; FAILED=1; }
ok()   { echo "  ✓ $*"; mark OK "$*"; }
step() { echo; echo "── $1/5 $2"; mark STEP "$1|5|$2"; }

FAILED=0

step 1 "사전 점검"

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
  NEED_MB=$(( $(file_size "$HERE/images.tar") / 1048576 * 3 ))
  AVAIL_MB=$(( $(avail_kb "$HERE") / 1024 ))
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

# 아키텍처가 다른 번들을 적재하면 컨테이너가 "exec format error" 로 죽는다.
# 그 문구만 보고 원인을 짚기는 어렵고, 그때는 이미 이미지를 다 푼 뒤다.
if [[ -f "$HERE/ARCH" ]]; then
  BUNDLE_ARCH="$(tr -d '[:space:]' < "$HERE/ARCH")"
  HOST_ARCH="$(host_arch)"
  if [[ "$BUNDLE_ARCH" == "$HOST_ARCH" ]]; then
    ok "아키텍처 $BUNDLE_ARCH"
  else
    fail "번들은 $BUNDLE_ARCH 인데 이 서버는 $HOST_ARCH 다 — 맞는 번들을 반입한다"
  fi
else
  # 구 번들에는 ARCH 파일이 없다. 막지 않되 모른다고 말한다 — "확인했다"와
  # "확인하지 않았다"를 같은 화면에 섞으면 안 된다.
  echo "  · 아키텍처 미표기 번들 (이 서버: $(host_arch))"
fi

step 2 "무결성 대조"
# 반입 매체는 손상되거나 바꿔치기될 수 있다. 대조 없이 적재하면 반쯤 깨진
# 이미지를 로드하고 원인 모를 장애를 쫓게 된다.
if [[ -f "$HERE/SHA256SUMS" ]]; then
  if mismatch="$(cd "$HERE" && sha256_verify SHA256SUMS)"; then
    ok "$(wc -l < "$HERE/SHA256SUMS" | tr -d ' ')개 파일 일치"
  else
    # 어느 파일이 깨졌는지 알려 준다. "무결성 실패"만 던지면 반입을 다시
    # 밟을지, 파일 하나만 다시 받을지 판단할 수 없다.
    [ -n "$mismatch" ] && echo "$mismatch" | sed 's/^/    /'
    fail "체크섬 불일치 — 이 번들로 설치하면 안 된다"
  fi
fi

if [[ "$FAILED" -ne 0 ]]; then
  echo
  echo "✗ 사전 점검 실패 — 설치를 진행하지 않는다." >&2
  mark ABORT "사전 점검 실패"
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "✓ 점검 통과 (dry-run — 적재·기동은 하지 않았다)"
  mark DONE "점검 통과"
  exit 0
fi

step 3 "이미지 적재"
docker load -i "$HERE/images.tar"

step 4 "기동"
mkdir -p "$DATA_DIR/audit" "$DATA_DIR/licenses"
chmod 700 "$DATA_DIR/audit"
if [[ -n "$BLOCKS" ]]; then
  # 블록 ID(STT-CORE)와 compose 서비스 이름(stt-core)은 대소문자만 다르다.
  # 인프라(redis·qdrant)와 의존 블록은 compose의 depends_on이 함께 올린다.
  SERVICES=()
  for b in $BLOCKS; do SERVICES+=("$(echo "$b" | tr '[:upper:]' '[:lower:]')"); done
  ok "선택 기동: ${SERVICES[*]}"
  (cd "$HERE" && docker compose -f docker-compose.yml up -d "${SERVICES[@]}")
else
  (cd "$HERE" && docker compose -f docker-compose.yml up -d)
fi

step 5 "자가진단"
"$HERE/selftest.sh"
mark DONE "설치 완료"

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
