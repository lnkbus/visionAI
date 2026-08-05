# 10. 스마트 회의록 데모 — 노트북에서 실제 음성으로

> 실제 마이크 → 자막 → 화자 라벨 → 회의록까지 한 번에 돌린다.
> **fake 어댑터가 아니라 진짜 음성인식**으로 돈다.

## 0. 먼저 알아야 할 것

기본 이미지에는 **인식 엔진이 들어 있지 않았다.** `faster-whisper`가 선택
의존성이라 `uv sync --package`가 설치하지 않았고, 그래서 `VAI_STT_ADAPTER`를
`faster_whisper`로 바꿔도 import에서 죽었다. 증상은 "설치는 됐는데 인식이
안 된다"였고, 그 사실은 기동 로그에만 남았다.

지금은 두 가지가 붙었다:

- 이미지 빌드에 `EXTRAS=whisper` — compose 기본값이다. 이미지에 어떤 엔진이
  들어갔는지는 라벨로 확인한다: `docker inspect --format '{{index .Config.Labels "ai.visionai.extras"}}'`
- **모델 가중치 반입 도구** `deploy/airgap/fetch_models.sh`

어댑터는 `local_files_only=True`로 모델을 연다. **컨테이너가 기동 중에 밖으로
나가지 않는다**는 뜻이고 폐쇄망 제품에서는 그게 맞다. 대신 가중치를 미리 갖다
놓아야 한다.

## 1. 준비 (한 번만, 네트워크 있는 곳에서)

```bash
# 모델 목록과 크기
deploy/airgap/fetch_models.sh --list

# 회의록 실사용 하한. Apple Silicon 16GB면 large-v3-turbo 도 올라간다.
uv run --with huggingface_hub bash deploy/airgap/fetch_models.sh --out models --stt small
```

| 모델 | 크기 | 어디에 |
|---|---|---|
| `tiny` | 75 MB | 기능 확인·CI. 한국어 정확도는 기대하지 않는다 |
| `base` | 142 MB | 데모 하한 |
| **`small`** | 484 MB | **회의록 실사용 하한 (기본값)** |
| `medium` | 1.5 GB | 상담 녹취 |
| `large-v3-turbo` | 1.6 GB | 품질/속도 균형이 가장 좋다 |

## 2. 기동

```bash
docker compose -f deploy/compose/docker-compose.yml up --build -d
```

첫 빌드는 오래 걸린다(엔진 포함). 이후에는 캐시가 받는다.

모델을 `models/` 말고 다른 곳에 뒀다면:

```bash
VAI_MODEL_DIR=/path/to/models \
VAI_STT_MODEL_PATH=/models/faster-whisper-large-v3-turbo \
  docker compose -f deploy/compose/docker-compose.yml up -d
```

## 3. 확인 — 회의 시작 전에

**순서대로 하고, 앞이 안 되면 뒤로 가지 않는다.**

```bash
# (1) 블록이 떴는가
deploy/airgap/selftest.sh

# (2) 인식 엔진이 실제로 도는가 — 운영 콘솔의 "블록 시험" 탭
open http://localhost:8097/console
```

콘솔에서 **STT-CORE 왕복 시험**을 누른다. `audio.segment → stt.delta` 가
초록이면 엔진이 살아 있는 것이다. `/readyz` 만 보고 판단하지 않는다 —
살아 있는데 아무것도 못 하는 상태가 이 제품의 주된 실패 모양이었다.

## 4. 회의록 화면

```
http://localhost:8094/minutes
```

1. **참석자 이름을 먼저 입력한다.** 화자 라벨이 `speaker_1`로 나오는 것을
   실명으로 바꾸는 자리다. 입력한 이름은 재기동을 넘어 남는다.
2. 마이크를 허용하고 회의를 시작한다.
3. 자막이 실시간으로 흐르고, 화자 라벨은 **뒤따라 붙는다** — 화자분리가
   인식보다 느려서 자막을 기다리게 하지 않기로 한 설계다.
4. 회의가 끝나면 세션을 종료한다. 요약 배치가 돌아 회의록이 만들어진다.

## 5. 이 데모에서 기대하지 말아야 할 것

정직하게 적어 둔다. 회의 자리에서 놀라지 않으려면 먼저 알아야 한다.

| 항목 | 실제 |
|---|---|
| **화자분리 정확도** | 기본 spectral 임베더는 **음역이 뚜렷이 다른 화자만** 가른다. 비슷한 목소리 둘은 못 가른다 — 신경망 임베더 반입이 선행되어야 한다 |
| **STT 정확도** | `small` 기준. 전문용어·상품명은 틀린다. 커스텀 사전(SCN-STUDIO)에 넣으면 나아진다 |
| **요약 품질** | LLM 게이트웨이가 echo 어댑터면 **프롬프트가 그대로 돌아온다**. 실제 요약은 sLLM 연결이 선행한다 |
| **지연** | CPU 추론이다. `small`이면 발화 후 1~3초, `large-v3-turbo`면 더 걸린다 |
| **동시 채널** | 노트북 1대는 성능 근거가 되지 않는다 |

숫자로 된 품질 근거는 아직 없다. `evalctl`의 음성 스위트로 재는 것이
다음 작업이며, 오디오 평가셋(Zeroth-Korean)이 선행한다.

## 6. 안 될 때

| 증상 | 먼저 볼 것 |
|---|---|
| 자막이 안 나온다 | 콘솔 → STT-CORE 왕복 시험. 실패하면 `docker compose logs stt-core` |
| `ModuleNotFoundError: faster_whisper` | 이미지에 엔진이 없다. `docker compose build --no-cache stt-core` |
| 모델을 못 찾는다 | `VAI_STT_MODEL_PATH`와 마운트 경로가 맞는지. 컨테이너 안 경로는 `/models/...` 다 |
| 인식이 영어로 나온다 | `VAI_STT_ADAPTER_CONFIG` 의 `language: ko` 확인 |
| 회의록이 비어 있다 | 세션이 실제로 종료됐는지(`session.closed`). 콘솔 → LLM-GW 직접 호출로 모델 연결 확인 |
