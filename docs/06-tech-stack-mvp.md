# 06. 기술 스택 & 개발 계획 (코딩 착수용)

## 1. 기술 스택 (사양서 v1.0 기준)

| 계층 | 선정 | 비고 |
|------|------|------|
| 언어/프레임워크 | **Python + FastAPI** (asyncio) — 코드는 3.11+ 지원, 컨테이너는 3.12 | 음성 스트리밍·AI 생태계 밀착. 고객사 기존 런타임(3.11)에도 얹을 수 있게 하한을 열어 둠 |
| 오디오 인입 | SIP/RTP 게이트웨이(AICC), WebSocket/gRPC/파일(회의) | G.711/PCM 8k/16k |
| VAD | **Silero VAD** + 200~500ms 청크 버퍼 | |
| STT | **Faster-Whisper** (기본) / **Triton Server** (대규모 애드온) | BaseSTTAdapter 핫스왑 |
| TTS | **CosyVoice / Kokoro** | BaseTTSAdapter, 200ms 청크 스트리밍 |
| 화자분리 | pyannote 계열 | MEETING 프로파일 |
| SLM (실시간 질의 추출) | 1.5B~3B / vLLM | < 200ms |
| sLLM (요약/분석) | 7B~14B INT4/FP8 / **vLLM** | 배치 경로. SaaS는 상용 API 어댑터 |
| 검색 | **Qdrant**(Dense) + BM25(Sparse) + **BGE-Reranker-v2** | 하이브리드 |
| 문서 파서 | **HWP**/PDF/DOCX 파서 | 공공(HWP) 필수 |
| 필터 | 정규식(Python/C++) + 로컬 NER | < 10ms Fast-Path |
| DB | **PostgreSQL 16** (업무 데이터) + Qdrant(벡터) | RLS 멀티테넌시 |
| 버스/캐시 | **Redis Streams** (이벤트 버스·세션) → 대규모 시 NATS 승격 | |
| 스토리지 | S3 호환 (SaaS: S3 / 온프렘: MinIO) — 오디오 원본 암호화 저장 | |
| 프론트엔드 | **TypeScript + Next.js** (상담원 WS·회의록·어드민), WebSocket 실시간 | |
| 배포 | Docker Compose(PoC) / **K3s 단일노드**(공공 소형) / K8s+Helm | 03 문서 |
| 코드 보호 | Cython 컴파일 + PyArmor (온프렘 릴리스 빌드) | |
| 암호화 | KCMVP 검증모듈(ARIA/AES-256) | |
| 관측성 | OpenTelemetry + Prometheus/Grafana/Loki, GPU 모니터링(DCGM) | |
| CI/CD | GitHub Actions: ruff·mypy·pytest·블록별 이미지 빌드·차트 패키징 | |

## 2. 모노레포 구조 (블록 = 최상위 단위)

`✅` = 구현 완료(Wave 1~3), 나머지는 예정.

```
visionAI/
├── blocks/                        # ★ 레고블록 — 1블록 = 1디렉토리 = 1이미지 = 1청구단위
│   ├── core-bus/  ✅              #   블록 내부 구조:
│   ├── core-gw/   ✅              #   src/ adapters/ contracts/ block.yaml tests/
│   ├── aud-vad/   ✅
│   ├── stt-core/  ✅
│   ├── flt-micro/ ✅
│   ├── rag-kb/    ✅
│   ├── rag-srch/  ✅
│   ├── ta-assist/ ✅
│   ├── llm-gw/    ✅
│   ├── core-lic/                  # (Wave 6 — 게이트 인터페이스는 libs/common에 선구현)
│   ├── core-sec/ · core-adm/      # (Wave 6)
│   ├── aud-rtp/ · aud-ws/         # (Wave 4)
│   ├── stt-trt/ · spk-dia/        # (Wave 4~5)
│   ├── llm-sum/                   # (Wave 4)
│   ├── ui-agent/ · ui-meet/       # (Wave 4~5, Next.js)
│   └── bot-voice/ · ava-counsel/  # (Wave 7)
├── libs/
│   ├── contracts/ ✅              # 이벤트·프로토콜·매니페스트 스키마 — 블록 간 유일한 공유물
│   ├── common/    ✅              # 설정·로깅·이벤트 버스 클라이언트·라이선스 게이트·워커 골격
│   └── retrieval/ ✅              # 청킹·임베딩·벡터/BM25 색인·리랭킹 (RAG 블록 공유)
├── deploy/
│   ├── docker/    ✅              # 전 블록 공용 Dockerfile (BLOCK 인자로 대상 지정)
│   ├── compose/   ✅              # PoC·데모용 단일 서버 구성
│   ├── charts/ · bundles/ · airgap/   # (Wave 6 — 03 문서)
├── tools/
│   ├── blockctl/  ✅              # 블록 매니페스트 검증·카탈로그 CLI
│   └── licgen/                    # .lic 발급기 (Wave 6, 사내용)
├── tests/         ✅              # 블록 조립 통합 테스트 + 실프로세스 스택 스모크
├── docs/
└── Makefile       ✅              # make check / up / demo
```

### 구현 현황 (Wave 1~3 완료)

| 항목 | 상태 |
|------|------|
| 이벤트 계약 v1 (`audio.in` → `audio.segment` → `stt.delta` → `filter.clean` → `assist.popup`) | ✅ |
| WebSocket 프로토콜 `/v1/audio/stream` (사양서 §4 규격) | ✅ |
| 세션 레지스트리 + Dual-Profile(AICC/MEETING) 컨텍스트 전파 | ✅ |
| VAD 발화 분할기 (패딩·interim·행오버·최대길이·채널 격리) | ✅ |
| STT 어댑터 ABC + fake/Faster-Whisper 어댑터, 레지스트리 핫스왑 | ✅ |
| PII 마스킹 + 컴플라이언스 룰 체크 (Fast-Path, 10ms 예산 테스트 포함) | ✅ |
| 조항 경계 존중 청킹 + 임베딩 어댑터(hashing/로컬 모델) | ✅ |
| 하이브리드 검색 (Qdrant Dense + BM25 Sparse RRF 융합) + 리랭킹 | ✅ |
| 맥락 기반 질의 추출(SLM + 어휘 폴백) → 지식 팝업, 1초 예산 | ✅ |
| LLM 프로바이더 추상화 (vLLM/상용 API/echo), 프로파일별 모델, 토큰 계측 | ✅ |
| JWT 인증 + 세션 스코프 WS 토큰, 테넌트 격리(컬렉션 분리) | ✅ |
| 마이크→자막→팝업 데모 페이지 (`/demo`) | ✅ |
| 블록 경계 CI 강제 (import-linter 4개 계약) | ✅ |
| 라이선스 게이트 인터페이스 (DRM 본체는 Wave 6) | 부분 |
| 문서 파서(PDF/DOCX/HWP) — 현재는 평문 수집만 | 예정 |
| LLM-SUM(요약)·AUD-RTP(전화)·UI-AGENT | 예정 |

### 알려진 제약

- **`memory` 벡터 저장소는 단일 프로세스 전용이다.** RAG-KB와 RAG-SRCH를 별도
  컨테이너로 띄우면 색인한 문서가 검색에 보이지 않는다. 다중 컨테이너 배포에는
  Qdrant를 쓴다(compose 기본값). 오설정 시 기동 로그에 경고가 남는다.
- **BM25 색인은 메모리 상주다.** 프로세스 재시작 시 `VAI_SRCH_WARM_TENANTS`
  또는 `/internal/v1/kb/{kb}/rebuild`로 복원해야 한다. 복원하지 않으면 검색이
  밀집 축만으로 도는 "절반만 동작하는" 상태가 되는데, 장애로 보이지 않아 더 위험하다.
- **한국어 토큰화는 어절 + 음절 바이그램**이다. 형태소 분석기(kiwi/mecab)는
  평가셋으로 이득을 측정한 뒤 어댑터로 교체한다.
- **추천 답변 생성은 아직 없다.** 팝업은 근거 원문을 그대로 보여준다 —
  없는 답을 지어내느니 원문이 낫다는 판단이며, sLLM 생성은 Wave 4다.

### 로컬 실행

```bash
make install          # uv 워크스페이스 동기화
make check            # 린트 + 블록 경계 + 타입 + 테스트 + 카탈로그 검증
make up && make demo  # compose 기동 후 http://localhost:8080/demo
```

`make test`는 Redis도 GPU도 요구하지 않는다(인메모리 버스 + fake 어댑터).
Redis가 떠 있으면 실프로세스 스택 스모크 테스트까지 함께 돈다.

### 블록 개발 규칙

1. **블록 간 코드 import 금지** — 공유는 `libs/` 아래 명시적으로 올린 것만 허용
   (`vai_contracts` 계약 · `vai_common` 런타임 인프라 · `vai_retrieval` 검색 인프라). CI에서 import-linter로 강제
2. **계약 우선**: 이벤트 스키마(AsyncAPI)·API(OpenAPI)를 먼저 정의 → 코드 생성. 계약 변경은 하위 호환 검사 통과 필수
3. **어댑터 플러그인**: 엔진 추가는 `adapters/`에 클래스 추가 + entry-point 등록. 코어 수정 발생 시 설계 리뷰
4. **환경 분기 금지**: `if is_onprem:` 대신 어댑터/설정 주입
5. **블록 인수 기준(DoD)**: 계약 테스트 + 단독 기동 smoke + 성능 예산(해당 시 지연 목표) 통과 → 그 시점부터 견적·청구 가능한 상태로 간주
6. **fake 어댑터 필수**: 모든 AI 엔진 블록은 결정적 fake 구현 제공 — GPU 없는 CI에서 전체 파이프라인 통합 테스트

## 3. 핵심 인터페이스 (요약)

- 이벤트 버스 토픽: `audio.in` → `audio.segment` → `stt.delta` → `filter.clean` → `assist.popup` / `session.closed` → `summary.done`
  - **Stream**(Redis Streams, 컨슈머 그룹): 위 토픽들. 타입별 스트림 1개 + 메시지의 `session_id` 필드로 세션 구분 — 세션마다 스트림을 만들면 워커가 신규 세션을 발견할 방법이 없다
  - **UI 채널**(Redis Pub/Sub): `vai:ui:{session_id}`. 접속 중인 화면으로의 팬아웃 전용 — 내구성이 불필요하고 UI 렌더 예산(<50ms)에 유리
- WebSocket `/v1/audio/stream` 프로토콜: [02 문서](02-architecture.md) §4 (사양서 원문 규격 준수)
- REST(관리): `/v1/kb/*`(지식), `/v1/sessions/*`(이력), `/v1/admin/*`(테넌트·룰셋·라이선스 상태), `/v1/rules/*`(컴플라이언스 룰셋 CRUD)

## 4. 개발 로드맵 — Wave × 사양서 Phase 매핑

사양서의 12주 Phase를 블록 Wave로 세분화한다. **각 Wave 완료 = 검수·기성 청구 가능 지점.**

| Wave | 기간(안) | 블록 | 완료 기준 (DoD) | 사양서 |
|------|----------|------|------------------|--------|
| **1. 뼈대** | W1~2 | CORE-BUS, CORE-GW, CORE-LIC(스텁) | 세션 생성→이벤트 왕복 E2E, compose 기동 | Ph.1 |
| **2. 음성 코어** | W2~4 | AUD-WS, AUD-VAD, STT-CORE | 마이크→실시간 자막 데모, Faster-Whisper 어댑터+fake 어댑터, 다국어 샘플 | Ph.1 |
| **3. 지능** | W4~7 | FLT-MICRO, RAG-KB, RAG-SRCH, TA-ASSIST, LLM-GW | PII 마스킹<10ms, 팝업 E2E<1초(p95), HWP/PDF 인덱싱, 평가셋 정확도 기준 | Ph.2 |
| **4. 제품화 A** | W7~9 | UI-AGENT, LLM-SUM, AUD-RTP | AICC 번들 통합 데모(전화 시뮬레이터), 표준요약 채택률 측정 체계 | Ph.3 |
| **5. 제품화 B** | W9~10 | SPK-DIA, UI-MEET | 회의 파일→화자별 회의록+Action Item, 회의록 번들 데모 | Ph.3 |
| **6. 패키징** | W10~12 | CORE-LIC(정식), CORE-SEC, CORE-ADM, airgap | H/W Fingerprint 검증, KCMVP 암호화, 에어갭 설치 리허설, PyArmor 빌드, GS인증 준비 | Ph.4 |
| **7. 확장** | Y2 | TTS-CORE→BOT-VOICE→AVA-COUNSEL | [04 문서](04-ai-avatar-counselor.md) 로드맵 | — |

### MVP 데모 시나리오 (Wave 4 종료 시)

> 전화 시뮬레이터로 상담 통화 재생 → 상담원 화면에 실시간 자막(고객/상담원 채널 분리) →
> 주민번호 발화 시 즉시 마스킹 표시 → "결제일 연기 되나요?" 발화 1초 내 약관 팝업+추천 답변 →
> 통화 종료 수 초 후 카테고리·표준 요약 자동 생성 → 어드민에서 GPU 사용률·지연 대시보드 확인

## 5. 품질·성능 검증 체계

| 항목 | 방법 |
|------|------|
| 지연 예산 회귀 | 각 블록 계약 테스트에 지연 assert(FLT<10ms, 검색<100ms, 리랭킹<80ms), 통합 p95<1초 — CI 야간 벤치 |
| STT 품질 | 도메인 오디오 평가셋(금융 상담 100통화, 회의 20건) WER/CER 회귀 측정 |
| RAG 품질 | 보험/카드 약관 Q&A 골든셋 100문항 — 인용 정확도·팝업 적합률 |
| 요약 품질 | 표준요약 골든셋 + 사람 평가 루브릭(주기 샘플링) |
| 부하 | 동시 채널 시뮬레이터(오디오 재생기) — 프로파일 S/M 사양 검증 리포트 |

## 6. 다음 스프린트 백로그 (Wave 4)

Wave 1~3은 완료되었다(§2 구현 현황). 다음 목표는 **AICC 패키지 통합 데모**다.

1. `LLM-SUM`: `session.closed` → 상담 카테고리 분류 + 표준 요약 템플릿 생성
2. `TA-ASSIST` 추천 답변 생성: 검색 근거 + sLLM으로 상담원용 답변 초안 (인용 강제)
3. `RAG-KB` 문서 파서 어댑터: PDF/DOCX/**HWP**(공공 필수)
4. `AUD-RTP`: SIP/RTP 인입, Stereo 채널 분리(고객/상담원)
5. `UI-AGENT`: Next.js 상담원 워크스페이스 (자막·팝업·컴플라이언스 경고·요약)
6. 평가셋 구축: 카드/보험 약관 Q&A 골든셋 100문항 → CI 야간 회귀
7. 형태소 분석기 어댑터 후보 검증 (평가셋으로 BM25 이득 측정 후 채택 결정)
8. `CORE-ADM` 초안: 테넌트별 컴플라이언스 룰셋 API (현재는 파일 주입)
