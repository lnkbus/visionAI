# 12. 엔진 선택과 규모 산정

> STT·화자분리·요약(sLLM)을 무엇으로 돌릴지, 그리고 상담석 수에서 장비를
> 어떻게 뽑는지. **어댑터는 이미 다 있다** — 이 문서는 무엇을 어떻게 고르는가다.

## 1. 원칙 — 엔진은 교체 대상이다

모든 엔진이 어댑터 뒤에 있다. 블록 코드는 어느 엔진인지 모르고, 바꾸는 것은
**환경변수 두 줄**이다. 그래서 "지금 뭘 쓰고 있는지"를 항상 확인할 수 있어야
한다 — 운영 콘솔의 **블록 시험** 탭이 그 자리다(`/readyz` 는 살아 있다는 것만
말한다).

| 블록 | 환경변수 | 값 |
|---|---|---|
| STT-CORE | `VAI_STT_ADAPTER` | `fake` · `faster_whisper` |
| SPK-DIA | `VAI_DIA_EMBEDDER` | `spectral` · `speechbrain` |
| LLM-GW | `VAI_LLM_ADAPTER` | `echo` · `openai_compatible` · `anthropic` |
| TTS-CORE | `VAI_TTS_ADAPTER` | `fake` · `neural` |

## 2. STT — 모델 선택과 유료 API

### 2.1 로컬 모델 (기본)

```bash
deploy/airgap/fetch_models.sh --out models --stt large-v3-turbo
```

| 모델 | 크기 | 어디에 |
|---|---|---|
| `small` | 484 MB | 회의록 실사용 하한 |
| `medium` | 1.5 GB | 상담 녹취 |
| `large-v3-turbo` | 1.6 GB | **품질/속도 균형이 가장 좋다** |
| `large-v3` | 3.1 GB | 최고 품질 |

받아 오는 저장소는 `fetch_models.sh` 안에 표로 적혀 있습니다(`--list` 로 봅니다).
이름에서 만들어 내지 않습니다 — **`large-v3-turbo` 만 Systran 이 아니라 제3자가
CTranslate2 로 변환해 올린 것**이기 때문입니다. 반입 심사에서 배포처를 묻는
현장이면 `large-v3`(Systran) 를 쓰십시오. 품질은 더 좋고, 대신 3.1GB 에 느립니다.

```yaml
VAI_STT_ADAPTER: faster_whisper
VAI_STT_MODEL_PATH: /models/faster-whisper-large-v3-turbo
VAI_STT_ADAPTER_CONFIG: '{"device":"auto","compute_type":"","language":"ko"}'
```

`device` 가 GPU 추론 전환의 전부다. 지연이 문제면 여기부터 본다.

| 값 | 뜻 |
|---|---|
| `auto` (기본) | 있으면 GPU, 없으면 CPU. **어느 쪽으로 갔는지 로그에 남는다** |
| `cuda` | 없으면 **기동을 거부한다.** GPU 로 견적을 낸 설치에서 쓴다 — 조용히 CPU 로 내려가면 "제안서엔 GPU, 현장은 CPU"가 되고 아무 데도 안 적힌다 |
| `cpu` | 그대로 |

**맥에서는 어떤 GPU 도 못 쓴다.** Apple Silicon 은 CTranslate2 에 Metal 백엔드가
없고(`mps` 를 받지 않는다), 인텔 맥은 NVIDIA 카드가 꽂혀 있어도 Docker Desktop 이
VM 안에서 돌아 GPU 통과 경로가 없다. 둘 다 `compute_type` 을 `int8` 로 둔다.

CPU 추론에서는 스레드 수가 곧 처리량이다. 한 장비에 워커를 여럿 띄우면
`cpu_threads` 로 나눠 준다 — 안 나누면 워커를 늘릴수록 느려지는 구간이 생긴다.

### 2.2 유료 API (구글 STT 등)

어댑터를 하나 더 등록하면 된다 — `blocks/stt-core/src/vai_stt_core/adapters/`
에 `BaseSTTAdapter` 구현을 두고 `_REGISTRY` 에 한 줄. 인터페이스는
`transcribe_stream(pcm, sample_rate) -> AsyncGenerator[SttResult]` 뿐이다.

**다만 폐쇄망에서는 성립하지 않는다.** 유료 API 는 외부로 나가야 하고, 그것이
가능한 고객사(클라우드·인터넷 구간 허용)에서만 선택지다. 그래서 기본값이 아니고,
**라이선스·감사 관점에서도 다르다** — 발화가 외부로 나가면 개인정보 위탁이
되므로 계약과 동의 절차가 선행한다. 이 사실을 화면에 적지 않으면 운영자가
무심코 켠다.

> 현재 상태: **인터페이스는 열려 있고 구글/클로바 어댑터는 아직 없다.**
> 필요 시 어댑터 하나(≈100줄)와 자격증명 주입 경로를 더한다.

## 3. 화자분리 — 신경망 임베더

기본 `spectral` 임베더는 **음역이 뚜렷이 다른 화자만** 가른다. 비슷한 목소리
둘은 못 가르며, 회의록에서는 그게 가장 흔한 상황이다.

```bash
deploy/airgap/fetch_models.sh --out models --spk      # ECAPA-TDNN, ~85MB
```

compose 기본값이 이미 `speechbrain` 이다. 이미지는 `EXTRAS=neural` 로 빌드되며
torch 가 딸려 온다(이미지가 커진다 — 끄려면 `VAI_SPK_EXTRAS=`).

**화자 번호는 자동으로 붙는다** (`speaker_1`, `speaker_2`, …). 등장 순서대로
매기고, 새 화자로 판정되면 번호를 하나 올린다. 실명은 **회의록 화면에서 사람이
입력**하며(UI-MEET), 입력한 이름은 Redis 에 남아 재기동·복제를 넘는다.

> 자동으로 실명을 알아내지 않는다. 성문 등록 없이는 불가능하고, 등록은 그 자체가
> 생체정보 수집이라 별도 동의 절차가 필요하다.

## 4. 요약 sLLM

LLM-GW 의 `openai_compatible` 어댑터가 **OpenAI 호환 엔드포인트면 무엇이든**
받는다. Ollama·LM Studio·vLLM 이 모두 그 형식이므로 별도 어댑터가 필요 없다.

```bash
# 맥에서 (Apple Silicon 은 Metal 가속, 인텔 맥은 CPU)
ollama serve
ollama pull qwen2.5:7b-instruct-q4_K_M
```

```yaml
VAI_LLM_ADAPTER: openai_compatible
VAI_LLM_BASE_URL: http://host.docker.internal:11434/v1
VAI_LLM_MODEL: qwen2.5:7b-instruct-q4_K_M
```

교체는 `VAI_LLM_MODEL` 한 줄이다. 서버에서 vLLM 으로 갈 때도 `BASE_URL` 만 바뀐다.

**16GB 맥 주의**: STT 와 sLLM 을 동시에 올리면 스왑이 난다. 그러면 측정한 지연이
모델 성능이 아니라 메모리 압박을 재게 된다. 데모에서는 **요약을 회의 종료 후**에
돌리므로 겹치지 않지만, 동시 실행이 필요하면 STT 를 `small` 로 내린다.
32GB 면 이 제약이 없다 — `large-v3-turbo` + 7B 를 함께 올려도 된다.

> `echo` 어댑터로 두면 **요약이 프롬프트를 그대로 돌려준다.** 콘솔의 LLM-GW
> 직접 호출 시험에서 그 사실이 즉시 보인다.

### 4.1 STT 가 먼저다 — 요약은 양보한다

한 장비에 둘을 올리면 메모리를 다툰다. **회의 중 자막이 끊기는 것과 회의록이
3분 늦는 것은 비용이 다르므로** 순서를 정해 둔다.

```
STT-CORE  ──인식 중──▶  vai:busy:stt (TTL 20초 하트비트)
                              │
LLM-SUM   ──세션 종료──▶ 표시가 있으면 ▶ status=waiting (화면에 표시)
                              │           ▼ 최대 180초 대기
                              └────────▶ status=running ▶ ready
```

| 설정 | 기본 | 뜻 |
|---|---|---|
| `VAI_STT_BUSY_SIGNAL` | `true` | 인식 중임을 알린다 |
| `VAI_SUM_YIELD_TO_STT` | `true` | 요약이 그 표시를 보고 미룬다 |
| `VAI_SUM_YIELD_MAX_WAIT_S` | `180` | 상한. 넘으면 **포기하고 진행한다** |

세 가지를 지킨다:

1. **영영 밀리지 않는다.** 통화가 끊이지 않는 상담센터에서는 "한가해질 때"가
   오지 않는다. 상한을 넘으면 그대로 진행하고 `yield_gave_up` 에 남긴다 —
   실패가 아니라 **증설 근거**다.
2. **기다린다는 사실이 보인다.** 회의록 화면이 `waiting` 을 "실시간 인식이
   끝나기를 기다리는 중"으로 적는다. 아무 표시가 없으면 사용자는 실패로 읽는다.
3. **끌 수 있다.** GPU 가 넉넉한 서버에서 이 직렬화는 손해다.

바쁨 표시는 **짧은 TTL 의 하트비트**다. 프로세스가 죽어도 20초 안에 저절로
풀린다 — 남으면 요약이 영원히 대기하고, 그건 되돌리기 어려운 종류의 정지다.
표시는 Redis 에 두므로 컨테이너·노드를 넘어 보인다.

## 5. 규모 산정 — 상담석에서 장비로

```bash
uv run blockctl capacity 1000            # 1,000석
uv run blockctl capacity 5000 --tts      # 음성봇 포함
uv run blockctl capacity 300 --json
```

**상담석 하나가 채널 하나가 아니다.** 상담사와 고객이 동시에 말하므로 인식은
**2채널**이 필요하다.

```
상담석 1,000석
  x 2채널(상담사+고객) x 동시 75% = 동시 1,500채널

  STT     1,500채널  GPU  24장   실시간 자막, 지연 우선
  화자분리  1,500채널  GPU  12장   인식보다 가볍다
```

이 숫자를 1,000으로 잡으면 도입 직후 절반이 인식되지 않고, 증상은 "가끔 상담사
말이 안 잡힌다"로 나타나 원인을 찾기 어렵다.

**동시 통화율 75%는 피크 기준이다.** 1,000석이 전부 동시에 통화하지는 않지만,
콜센터는 아침 오픈과 점심 직후에 몰린다 — 평균 가동률로 사이징하면 그 시간대에
막힌다.

### 확장 구조

| 블록 | 확장 | 이유 |
|---|---|---|
| STT-CORE · FLT-MICRO · TA-ASSIST | **수평 (무상태)** | 컨슈머 그룹이 부하를 나눈다. replica 를 늘리면 그만큼 는다 |
| SPK-DIA | 수평 | 세션 상태는 있으나 세션이 워커에 고정된다 |
| **LLM-SUM** | **수직만** | 단일 라이터 — 파일 저장소를 여러 파드가 공유하면 인덱스가 어긋난다. 차트가 `replicas: 1` + `Recreate` 로 강제한다 |
| Redis | 별도 사이징 | 1,000석 이상은 스트림 처리량이 병목이 된다 |

### 라이선스와 같은 숫자를 쓴다

```bash
uv run blockctl capacity 1000 --json | python3 -c "import json,sys; print(json.load(sys.stdin)['channels'])"
# 1500
```

라이선스 동시 채널은 **피크를 막는 것**이라 여유를 두지 않는다. 여유를 두면
초과 사용이 청구되지 않고, 부족하면 피크에 429가 난다.

## 6. 이 문서의 숫자는 실측이 아니다

`blockctl capacity` 의 GPU당 채널 값은 **설계 가정**이다. 실서버 성능 시험 전까지
견적의 출발점으로만 쓰고, 제안서에 넣을 때는 그 사실을 함께 적는다
(`docs/07 §3.2` 의 미완 항목).

측정이 되면 `tools/blockctl/src/blockctl/capacity.py` 의 `PROFILES` 를 실측값으로
갈아 끼운다 — 그 한 곳만 고치면 모든 견적이 따라온다.
