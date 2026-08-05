#!/usr/bin/env bash
# 설치 자가진단 — 기동 직후와 정기 점검에 쓴다.
#
# 폐쇄망에서는 "설치가 됐다"를 공급사가 확인해 줄 수 없다. 검수자가 이 스크립트
# 출력 하나로 판단할 수 있어야 하며, **실패했을 때 어디를 보라고 알려 줘야** 한다.
# 실패 목록만 던지는 진단은 결국 공급사 전화로 이어진다.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=portable.sh
. "$HERE/portable.sh"

HOST="${VAI_SELFTEST_HOST:-localhost}"
DEADLINE="${VAI_SELFTEST_TIMEOUT:-90}"

# 블록 ID를 변수 이름으로 바꿔 간접 확장(${!v})으로 읽는다. shellcheck는 그
# 참조를 보지 못해 전부 미사용으로 본다 — 연관배열은 bash 4+ 전용이라 macOS
# 기본 bash에서 쓸 수 없다. 묶음 하나로 만들어 지시어가 표 전체를 덮게 한다.
# shellcheck disable=SC2034
{
  PORTS_CORE_BUS=8081 PORTS_CORE_GW=8080 PORTS_AUD_VAD=8082 PORTS_STT_CORE=8083
  PORTS_FLT_MICRO=8084 PORTS_LLM_GW=8085 PORTS_RAG_KB=8086 PORTS_RAG_SRCH=8087
  PORTS_TA_ASSIST=8088 PORTS_LLM_SUM=8089 PORTS_SCN_STUDIO=8090 PORTS_UI_AGENT=8091
  PORTS_AUD_RTP=8092 PORTS_SPK_DIA=8093 PORTS_UI_MEET=8094 PORTS_CORE_LIC=8095
  PORTS_CORE_SEC=8096 PORTS_CORE_ADM=8097
}

port_for() { local v="PORTS_${1//-/_}"; echo "${!v:-}"; }

if [[ -f "$HERE/plan.json" ]]; then
  read_into BLOCKS < <(python3 -c '
import json, sys
print("\n".join(json.load(open(sys.argv[1]))["blocks"]))' "$HERE/plan.json")
else
  BLOCKS=(CORE-BUS CORE-LIC CORE-SEC CORE-ADM)
fi

PASS=0 WARN=0 FAIL=0
pass() { echo "  ✓ $*"; PASS=$((PASS+1)); }
warn() { echo "  ! $*"; WARN=$((WARN+1)); }
bad()  { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

echo "── 블록 기동 확인 (최대 ${DEADLINE}초 대기)"
started=$(date +%s)
for block in "${BLOCKS[@]}"; do
  port="$(port_for "$block")"
  if [[ -z "$port" ]]; then warn "$block: 포트를 모른다 (진단 건너뜀)"; continue; fi

  body=""
  while :; do
    body="$(curl -sf --max-time 3 "http://$HOST:$port/readyz" 2>/dev/null)" && break
    (( $(date +%s) - started > DEADLINE )) && break
    sleep 2
  done

  if [[ -z "$body" ]]; then
    # 어디를 보라고 알려 준다. "실패"만 던지면 결국 공급사 전화로 이어진다.
    bad "$block (:$port) 응답 없음 → docker compose logs $(echo "$block" | tr '[:upper:]' '[:lower:]')"
  elif [[ "$body" == *'"license_expired":true'* ]]; then
    warn "$block 기동했으나 라이선스 만료 상태 → 콘솔 http://$HOST:8097/console"
  else
    pass "$block (:$port)"
  fi
done

echo
echo "── 라이선스"
lic="$(curl -sf --max-time 3 "http://$HOST:8095/internal/v1/license" 2>/dev/null || true)"
if [[ -z "$lic" ]]; then
  bad "CORE-LIC 응답 없음 → docker compose logs core-lic"
elif [[ "$lic" == *'"installed":false'* ]]; then
  # 최초 설치에서는 정상 상태다. 실패로 표시하면 검수자가 불필요하게 멈춘다.
  warn "라이선스 미설치 — 발급 절차가 남았다 (install.sh 안내 참조)"
else
  if [[ "$lic" == *'"signature_verified":false'* ]]; then
    bad "라이선스 서명 검증이 꺼져 있다 — 릴리스 빌드가 아니다"
  else
    pass "라이선스 설치·검증 완료"
  fi
fi

echo
echo "── 감사 로그"
chain="$(curl -sf --max-time 30 "http://$HOST:8096/internal/v1/audit/verify" 2>/dev/null || true)"
if [[ -z "$chain" ]]; then
  bad "CORE-SEC 응답 없음 → docker compose logs core-sec"
elif [[ "$chain" == *'"intact":true'* ]]; then
  pass "무결성 통과"
else
  bad "무결성 손상 → 콘솔의 감사 탭에서 끊긴 지점 확인"
fi

crypto="$(curl -sf --max-time 3 "http://$HOST:8096/internal/v1/crypto/status" 2>/dev/null || true)"
if [[ "$crypto" == *'"enabled":true'* ]]; then
  pass "저장 암호화 활성"
else
  warn "저장 암호화 꺼짐 — 평문 저장 (VAI_SEC_MASTER_KEY_PATH 미설정)"
fi

echo
echo "통과 $PASS · 경고 $WARN · 실패 $FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
