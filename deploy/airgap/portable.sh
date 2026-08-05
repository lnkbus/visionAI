#!/usr/bin/env bash
# 설치 스크립트 이식 계층 — GNU coreutils가 없는 곳에서도 같은 답을 낸다.
#
# 설치기가 리눅스에서만 도는 것은 상관없다고 생각하기 쉽다. 고객사 서버는
# 리눅스니까. 문제는 **개발자와 영업이 맥에서 번들을 열어 본다**는 것이고,
# 그때 `stat: illegal option -- c` 같은 문구를 만나면 번들이 깨진 줄 안다.
# 실제로는 도구가 다를 뿐이다.
#
# 여기서 흡수하는 차이:
#
#   GNU (리눅스)              BSD (macOS)              쓰는 곳
#   stat -c%s                 stat -f%z                디스크 여유 계산
#   sha256sum                 shasum -a 256            무결성 대조
#   df -Pm                    df -Pk (POSIX)           디스크 여유
#   mapfile -t                (bash 4+ 전용)           목록 읽기
#   sort -z                   (GNU 전용)               체크섬 목록 정렬
#
# **bash 3.2에서 돈다.** macOS가 기본으로 주는 bash가 3.2이고, 고객사 서버에
# 신형 bash가 있으리라 가정할 이유도 없다. 그래서 mapfile·연관배열·${var,,}를
# 쓰지 않는다. tests/test_installer_portability.py가 이 약속을 검사한다.
#
# Windows는 WSL2로 받는다. 네이티브 지원은 리눅스 컨테이너 이미지를 그대로
# 쓸 수 없어 사실상 두 번째 제품이 된다.

# ── 파일 크기 (바이트) ──────────────────────────────────────────────────────
file_size() {
  # wc -c 는 어디에나 있지만 큰 파일에서 전체를 읽는다. stat 을 먼저 시도한다.
  stat -c%s "$1" 2>/dev/null ||
    stat -f%z "$1" 2>/dev/null ||
    wc -c < "$1" | tr -d ' '
}

# ── 디스크 여유 (KB) ────────────────────────────────────────────────────────
avail_kb() {
  # -P 는 POSIX 출력 형식(한 줄), -k 는 1024바이트 단위. 둘 다 POSIX 필수라
  # GNU·BSD가 같게 동작한다. -m 은 BSD에 없는 구현이 있어 쓰지 않는다.
  df -Pk "$1" | awk 'NR==2 {print $4}'
}

# ── SHA-256 ────────────────────────────────────────────────────────────────
_sha_cmd=""
sha_cmd() {
  if [ -z "$_sha_cmd" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      _sha_cmd="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
      _sha_cmd="shasum -a 256"
    else
      echo "sha256sum 도, shasum 도 없다 — 무결성을 대조할 수 없다" >&2
      return 1
    fi
  fi
  echo "$_sha_cmd"
}

sha256_of() {
  $(sha_cmd) "$1" | awk '{print $1}'
}

# 표준입력으로 받은 파일 목록의 체크섬을 만든다.
sha256_list() {
  local cmd
  cmd="$(sha_cmd)" || return 1
  # xargs 로 넘기면 인자 길이 상한에 걸릴 수 있고 -0 지원도 갈린다.
  # 한 줄씩 도는 편이 느리지만 어디서나 같게 동작한다.
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    $cmd "$path"
  done
}

# 체크섬 대조. 일치하면 0, 아니면 1. 불일치 파일명은 그대로 흘려보낸다 —
# "무결성 실패"만 알려 주는 진단은 어느 파일이 깨졌는지 못 알려 준다.
sha256_verify() {
  local cmd out status
  cmd="$(sha_cmd)" || return 1
  # 먼저 받아 두고 상태를 즉시 붙잡는다.
  #
  # 처음에는 `$cmd -c "$1" | grep -v ': OK$' || true` 뒤에 PIPESTATUS[0]을
  # 돌려주게 짰다가 무결성 검사가 **항상 통과**했다. `|| true`의 `true`가
  # 단순 명령이라 PIPESTATUS를 (0)으로 덮어쓴다. 체크섬을 깨뜨려도 "일치"가
  # 나왔고, 그것이 바로 이 검사가 막아야 하는 상황이다.
  out="$($cmd -c "$1" 2>&1)"
  status=$?
  # --quiet 는 GNU 전용이다. 성공 줄만 걸러내 같은 효과를 낸다.
  printf '%s\n' "$out" | grep -v ': OK$' || true
  return $status
}

# ── 목록 읽기 (mapfile 대체) ────────────────────────────────────────────────
# mapfile 은 bash 4+ 전용이라 macOS 기본 bash(3.2)에서 없는 명령이 된다.
# 없는 명령은 조용히 빈 배열을 만들지 않고 스크립트를 죽이지만, set -e 가
# 없는 곳에서는 빈 목록으로 계속 도는 쪽이 더 위험하다.
#
#   read_into BLOCKS < <(명령)
read_into() {
  local __name="$1" __line
  eval "$__name=()"
  while IFS= read -r __line; do
    [ -n "$__line" ] || continue
    eval "$__name+=(\"\$__line\")"
  done
}

# ── 정렬된 파일 목록 (sort -z 대체) ─────────────────────────────────────────
# 번들 파일명은 우리가 만든다 — 개행이 든 이름이 생길 수 없으므로 NUL 구분이
# 필요 없다. 대신 로케일을 고정한다: 로케일이 다르면 정렬 순서가 달라져
# 같은 내용에서 다른 SHA256SUMS 가 나온다.
sorted_files() {
  find "${1:-.}" -type f ! -name SHA256SUMS | LC_ALL=C sort
}

# ── 아키텍처 ────────────────────────────────────────────────────────────────
# uname -m 의 표기가 제각각이다(x86_64 / amd64 / aarch64 / arm64). docker 표기로
# 맞춘다 — 번들의 ARCH 파일과 대조해야 하기 때문이다.
host_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) uname -m ;;
  esac
}
