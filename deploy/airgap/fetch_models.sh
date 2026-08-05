#!/usr/bin/env bash
# 모델 가중치 반입 — **네트워크가 있는 곳에서** 실행한다.
#
# 어댑터는 `local_files_only=True` 로 모델을 연다. 컨테이너가 기동 중에 밖으로
# 나가지 않는다는 뜻이고, 폐쇄망 제품에서는 그게 맞다. 대신 **가중치를 미리
# 갖다 놓아야 하며, 그 일을 하는 것이 이 스크립트다.**
#
# 이것이 없으면 무슨 일이 생기냐면: 이미지에 엔진은 들어 있는데 모델이 없어
# 기동에 실패하고, 로그에는 huggingface 경로가 찍힌다. 폐쇄망 담당자가 그
# 문구를 보고 할 수 있는 일이 없다.
#
#   ./fetch_models.sh --out dist/models                      # 기본(회의록·상담)
#   ./fetch_models.sh --out dist/models --stt large-v3-turbo # 모델 지정
#   ./fetch_models.sh --out dist/models --stt tiny           # 데모·시험용
#   ./fetch_models.sh --out dist/models --spk                # 화자분리 신경망 임베더
#   ./fetch_models.sh --out dist/models --stt small --spk    # 함께
#
# 받은 디렉토리를 컨테이너에 마운트한다:
#   VAI_STT_MODEL_PATH=/models/faster-whisper-<이름>
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=portable.sh
. "$HERE/portable.sh"

OUT_DIR=""
STT_MODEL="small"
SPK=0
SPK_MODEL="speechbrain/spkrec-ecapa-voxceleb"
WANT_STT=1
LIST_ONLY=0

usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --stt) STT_MODEL="$2"; shift 2 ;;
    --spk) SPK=1; shift ;;                     # 화자분리 신경망 임베더(ECAPA-TDNN)
    --spk-model) SPK=1; SPK_MODEL="$2"; shift 2 ;;
    --no-stt) WANT_STT=0; shift ;;
    --list) LIST_ONLY=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; usage 1 ;;
  esac
done

# 크기를 함께 적는다. 반입 매체 용량은 미리 알아야 하고, "받아 보니 3GB"는
# 폐쇄망에서 되돌리기 어렵다.
if [[ "$LIST_ONLY" -eq 1 ]]; then
  cat <<'MODELS'
STT (faster-whisper) — 이름 / 대략 크기 / 쓸 자리

  tiny              75 MB   기능 확인·CI 용. 한국어 정확도는 기대하지 않는다
  base             142 MB   데모용 하한
  small            484 MB   회의록 실사용 하한 (권장 기본값)
  medium           1.5 GB   상담 녹취
  large-v3-turbo   1.6 GB   품질/속도 균형이 가장 좋다 (Apple Silicon 권장)
  large-v3         3.1 GB   최고 품질, 16GB 노트북에서는 빠듯하다

화자분리 임베더 (--spk)

  spkrec-ecapa-voxceleb   ~80 MB
      기본 spectral 임베더는 **음역이 뚜렷이 다른 화자만** 가른다. 비슷한
      목소리 둘은 못 가르며, 회의록에서는 그것이 가장 흔한 상황이다.
      torch 를 함께 반입해야 한다(이미지 EXTRAS=neural).
MODELS
  exit 0
fi

[[ -n "$OUT_DIR" ]] || { echo "--out 이 필요하다" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 가 필요하다" >&2; exit 1; }

mkdir -p "$OUT_DIR"
TARGET="$OUT_DIR/faster-whisper-$STT_MODEL"

if [[ "$WANT_STT" -eq 1 ]]; then
echo "▸ STT 모델 내려받기: $STT_MODEL"
echo "  (네트워크가 있는 곳에서 한 번만 한다 — 고객사 서버에서 돌리는 스크립트가 아니다)"

python3 - "$STT_MODEL" "$TARGET" <<'PY'
import sys

model, target = sys.argv[1], sys.argv[2]
try:
    from huggingface_hub import snapshot_download
except ImportError:
    sys.exit(
        "huggingface_hub 이 없다 — `uv run --with huggingface_hub "
        "deploy/airgap/fetch_models.sh ...` 또는 pip install huggingface_hub"
    )

# faster-whisper 가 이름을 이 저장소로 푼다. 여기서 같은 규칙을 쓴다.
repo = f"Systran/faster-whisper-{model}"
path = snapshot_download(
    repo,
    local_dir=target,
    # 토크나이저·설정·가중치만. 예제와 문서는 반입 대상이 아니다.
    allow_patterns=["*.bin", "*.json", "*.txt", "*.model"],
)
print(f"  ✓ {path}")
PY

echo "▸ 체크섬"
(cd "$TARGET" && sorted_files . | sha256_list > SHA256SUMS)
fi

# 화자분리 신경망 임베더. 기본 spectral 임베더는 음역이 뚜렷이 다른 화자만
# 가르므로, 회의록 품질을 내려면 이것이 사실상 필수다.
if [[ "$SPK" -eq 1 ]]; then
  SPK_TARGET="$OUT_DIR/$(basename "$SPK_MODEL")"
  echo "▸ 화자분리 임베더 내려받기: $SPK_MODEL"
  python3 -c "
import sys
try:
    from huggingface_hub import snapshot_download
except ImportError:
    sys.exit('huggingface_hub 이 없다 — uv run --with huggingface_hub 로 실행한다')
path = snapshot_download(sys.argv[1], local_dir=sys.argv[2],
                         allow_patterns=['*.ckpt', '*.yaml', '*.txt'])
print('  ✓ ' + path)
" "$SPK_MODEL" "$SPK_TARGET"
  (cd "$SPK_TARGET" && sorted_files . | sha256_list > SHA256SUMS)
  echo "  ✓ 임베더: $SPK_TARGET ($(du -sh "$SPK_TARGET" | cut -f1))"
  echo
  echo "  컨테이너에 마운트한다:"
  echo "    -e VAI_SPK_EMBEDDER=speechbrain"
  echo "    -e VAI_SPK_EMBEDDER_MODEL=/models/$(basename "$SPK_MODEL")"
  echo
fi

[[ "$WANT_STT" -eq 1 ]] || exit 0
SIZE="$(du -sh "$TARGET" | cut -f1)"
echo
echo "✓ 모델: $TARGET ($SIZE)"
echo
echo "  컨테이너에 마운트한다:"
echo "    -v \"\$(pwd)/$TARGET:/models/faster-whisper-$STT_MODEL:ro\""
echo "    -e VAI_STT_ADAPTER=faster_whisper"
echo "    -e VAI_STT_MODEL_PATH=/models/faster-whisper-$STT_MODEL"
echo
echo "  폐쇄망이면 이 디렉토리를 번들과 함께 반입한다."
