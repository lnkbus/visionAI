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

### 1.0 맥에서 할 때 — 먼저 챙길 것

| | |
|---|---|
| **Docker Desktop 메모리** | 설정 → Resources → Memory를 **최소 8GB, 권장 12GB**. 기본값(보통 4~8GB)이면 STT 모델을 올리다 컨테이너가 조용히 죽는다 |
| **디스크** | 이미지 + 모델로 **15GB 이상** 비워 둔다 |
| **아키텍처** | Apple Silicon(arm64)은 그대로 빌드된다. 로제타 에뮬레이션이 아니다 |
| **최초 빌드 시간** | 블록 21개 + 인식 엔진. **회의 직전에 하지 않는다** — 전날 한 번 돌려 둔다 |
| **마이크** | `http://localhost` 은 보안 컨텍스트로 취급되어 브라우저가 마이크를 허용한다. Safari보다 Chrome이 덜 까다롭다 |

```bash
# 모델 목록과 크기
deploy/airgap/fetch_models.sh --list

# 회의록 실사용 하한. Apple Silicon 16GB면 large-v3-turbo 도 올라간다.
uv run --with huggingface_hub bash deploy/airgap/fetch_models.sh --out models --stt small

# 화자분리 신경망 임베더(ECAPA-TDNN, ~85MB). compose 기본값이 이걸 쓴다.
uv run --with huggingface_hub bash deploy/airgap/fetch_models.sh --out models --spk --no-stt
```

**임베더 모델을 안 받았으면 SPK-DIA 가 기동에 실패한다.** 모델 없이 돌리려면
`VAI_DIA_EMBEDDER=spectral` 로 내린다 — 대신 음역이 뚜렷이 다른 화자만 갈린다.

| 모델 | 크기 | 어디에 |
|---|---|---|
| `tiny` | 75 MB | 기능 확인·CI. 한국어 정확도는 기대하지 않는다 |
| `base` | 142 MB | 데모 하한 |
| **`small`** | 484 MB | **회의록 실사용 하한 (기본값)** |
| `medium` | 1.5 GB | 상담 녹취 |
| `large-v3-turbo` | 1.6 GB | 품질/속도 균형이 가장 좋다 |

### 1.1 요약을 진짜로 나오게 하려면 (맥 GPU)

compose 기본값은 `VAI_LLM_ADAPTER=echo` 다. **echo 면 요약이 프롬프트를 그대로
돌려준다** — 회의록 검증이 목적이라면 이걸 먼저 바꾼다.

```bash
# 맥에서 (Metal 가속)
brew install ollama && ollama serve &
ollama pull qwen2.5:7b-instruct-q4_K_M
```

```bash
# compose 를 띄울 때
VAI_LLM_ADAPTER=openai_compatible \
VAI_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
VAI_LLM_MODEL=qwen2.5:7b-instruct-q4_K_M \
  docker compose -f deploy/compose/docker-compose.yml up -d llm-gw
```

16GB 맥에서 STT 와 sLLM 을 **동시에** 올리면 스왑이 난다. 그래서 요약은 회의
종료 후에 돌게 해 뒀다(STT 우선, docs/12 §4.1) — 회의 중에는 겹치지 않는다.

## 2. 기동

```bash
docker compose -f deploy/compose/docker-compose.yml up --build -d
docker compose -f deploy/compose/docker-compose.yml ps    # 포트는 여기가 기준이다
```

첫 빌드는 오래 걸린다(엔진 포함). 이후에는 캐시가 받는다.

| 화면 | 주소 |
|---|---|
| 스마트 회의록 | <http://localhost:8094/minutes> |
| 운영 콘솔 | <http://localhost:8097/console> |
| 저작 콘솔 | <http://localhost:8090/console> |
| 상담원 워크스페이스 | <http://localhost:8091/workspace> |
| 고객 데모(마이크→자막) | <http://localhost:8080/demo> |

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

## 7. 설치 화면까지 시연하려면 (고객사 설치 경로)

위(§2)는 **개발 경로**다. 고객사에서 실제로 하는 일은 다르다 — 인터넷이 없는
서버에 USB로 번들을 들고 들어가 설치기를 돌린다. 그 과정을 그대로 보여 준다.

```bash
# ① 번들 만들기 (맥에서 시연할 것이므로 arm64)
deploy/airgap/build_bundle.sh --block UI-MEET --block SPK-DIA --block STT-CORE \
  --arch arm64 --out dist/

# ② 반입한 척 풀기
tar -xzf dist/visionai-0.1.0.tar.gz -C /tmp && cd /tmp/visionai-0.1.0

# ③ 반입 전 점검만 (docker 를 건드리지 않는다)
./install.sh --dry-run

# ④ 브라우저 설치 마법사
./install.sh --gui        # http://127.0.0.1:8099
```

시연에서 짚을 것:

| | |
|---|---|
| **모듈 선택** | 화면에서 블록을 골라 설치한다 — 고객사가 산 블록만 들어간다 |
| **프로그레스** | 5단계(사전 점검 → 무결성 → 적재 → 기동 → 자가진단)가 실시간으로 흐른다 |
| **앞이 실패하면 뒤로 안 간다** | 설치 중 실패는 흔하고, 진짜 문제는 *어디까지 됐는지 모르는 실패*다 |
| **`--dry-run` 은 건너뛴 단계를 "건너뜀"으로 적는다** | ✓ 로 보이면 점검만 한 것을 설치했다고 오해한다 |
| **127.0.0.1 에만 붙는다** | 마법사는 인증 없이 docker 를 조작한다. 외부에 열면 그 자체가 원격 실행 통로다 |

설치 단계는 마법사가 다시 구현하지 않고 `install.sh` 를 몰아준다. 두 번
구현하면 둘이 갈라지고, 갈라진 사실은 현장에서 한쪽만 실패할 때 드러난다.

> 번들 빌드는 이미지를 `docker save` 로 담으므로 시간이 걸린다. 블록을 좁혀
> (`--block`) 만드는 편이 시연에는 낫다 — 21개 전부 담으면 수 GB가 된다.

### 7.1 모델은 번들에 없다 — 미리 놓는다

번들은 이미지만 담는다. 모델 가중치는 크고(수 GB) 고객사마다 고르는 것이
다르므로 **별도 반입**이다. 그래서 설치 직후 자가진단에서 STT-CORE·SPK-DIA 가
"응답 없음"으로 나오는 것이 정상 경로다 — 모델을 놓고 그 둘만 다시 띄운다.

```bash
# 번들을 푼 자리에서
mkdir -p models   # ← fetch_models.sh 로 받은 것을 여기 둔다
VAI_MODEL_DIR=$(pwd)/models docker compose up -d stt-core spk-dia
```

모델 없이 화면만 보여 줄 거라면 두 블록을 가벼운 설정으로 내린다:

```bash
VAI_STT_ADAPTER=fake VAI_DIA_EMBEDDER=spectral docker compose up -d stt-core spk-dia
```

> `fake` 어댑터는 **인식하지 않는다.** 화면 흐름만 보여 줄 때만 쓰고,
> 회의록 품질을 보여 줄 자리에서는 쓰지 않는다.

## 8. 회의 당일 순서 (권장)

```
전날  ① 모델 반입 (--stt small --spk)   ② docker compose up --build -d
      ③ Ollama 로 요약 붙이기            ④ 운영 콘솔에서 STT-CORE 왕복 시험 초록 확인
당일  ⑤ 회의록 화면에서 참석자 이름 입력  ⑥ 회의 진행
      ⑦ 세션 종료 → 요약 대기 표시 → 회의록
      ⑧ 운영 콘솔 [서비스 통계]에서 방금 회의가 숫자로 잡히는지 확인
```

⑧ 이 시연의 마무리로 좋다 — **방금 한 회의가 그대로 지표가 되는 것**을 보여 준다.
