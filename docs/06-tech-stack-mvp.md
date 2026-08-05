# 06. 기술 스택 & MVP 개발 계획 (코딩 착수용)

## 1. 기술 스택 선정

| 계층 | 선정 | 선정 이유 |
|------|------|-----------|
| 백엔드 | **Python 3.12 + FastAPI** | AI 생태계 밀착(LLM/임베딩/음성 라이브러리), 비동기 스트리밍(SSE/WebSocket) 성숙, 채용 풀 |
| AI 오케스트레이션 | 자체 경량 파이프라인 (+ 필요시 LangGraph 수준의 상태 그래프) | 프레임워크 종속 최소화 — 감사 기록·가드레일을 1급 시민으로 직접 구현 |
| LLM (SaaS) | Claude API (claude-sonnet-5 기본, 요약 등 경량 작업은 claude-haiku-4-5) | 한국어 품질, 긴 컨텍스트, 무학습 계약 |
| LLM (온프렘) | vLLM + 오픈 웨이트 한국어 강 모델(GPU 프로파일별 7B~70B급) | OpenAI 호환 API로 게이트웨이 추상화 용이 |
| 임베딩 | SaaS: API / 온프렘: 로컬 한국어 임베딩 모델 서빙 | 환경별 Provider 교체 |
| DB | **PostgreSQL 16 + pgvector** | 업무 데이터·벡터 검색 단일화(운영 단순), RLS 멀티테넌시, 온프렘 반입 용이 |
| 캐시/큐 | **Redis** (+ 작업큐: arq/Celery) | 세션 상태, 임베딩 작업큐 |
| 스토리지 | S3 호환 (SaaS: S3 / 온프렘: MinIO) | 단일 코드 경로 |
| 프론트엔드 | **TypeScript + React(Next.js)** — 관리자 콘솔·상담원 워크스페이스 / 채팅 위젯은 경량 임베드 스크립트 | 표준 생태계 |
| 실시간 | WebSocket(채팅), SSE(스트리밍 응답), WebRTC+SFU(Phase 3, LiveKit 계열) | 단계별 도입 |
| 배포 | Docker + Helm/K8s, 개발·PoC는 docker-compose | 03 문서 참조 |
| 관측성 | OpenTelemetry + Prometheus/Grafana + Loki | 온프렘 번들 동일 |
| CI/CD | GitHub Actions: lint(ruff)·type(mypy)·test(pytest)·이미지 빌드·차트 패키징 | 모노레포 단일 파이프라인 |

## 2. 모노레포 구조 (제안)

```
visionAI/
├── apps/
│   ├── api-core/            # FastAPI 모놀리스: auth, tenant, conversation, routing, admin API
│   │   ├── src/core/        #   도메인 모델·서비스 (모듈 경계 엄격)
│   │   ├── src/api/         #   라우터 (v1)
│   │   └── tests/
│   ├── ai-orchestrator/     # 대화 파이프라인 워커 (전처리→RAG→LLM→가드레일→후처리)
│   ├── knowledge-worker/    # 문서 파싱·청킹·임베딩 배치 워커
│   └── web/                 # Next.js: 관리자 콘솔 + 상담원 워크스페이스
│       └── packages/widget/ # 임베드형 채팅 위젯 (경량 번들)
├── libs/
│   ├── llm-gateway/         # Provider 추상화 (anthropic / vllm / ...)
│   ├── schemas/             # Pydantic 모델·API 계약 (OpenAPI 소스)
│   └── common/              # 로깅, 텔레메트리, PII 마스킹, RLS 헬퍼
├── deploy/                  # Helm 차트, values-*, airgap/, compose/
├── docs/                    # 본 설계 문서
└── Makefile                 # dev up/down, test, lint, seed
```

## 3. 핵심 API 초안 (v1)

```
# 대화 (위젯/채널용)
POST   /v1/conversations                      # 세션 시작 {channel, visitor_meta}
POST   /v1/conversations/{id}/messages        # 발화 전송 → SSE 스트리밍 응답
POST   /v1/conversations/{id}/handoff         # 상담원 전환 요청
POST   /v1/conversations/{id}/feedback        # 도움됨/안됨 피드백

# 상담원 워크스페이스
GET    /v1/agent/queue                        # 배분 대기열 (WebSocket 구독)
POST   /v1/agent/conversations/{id}/accept
GET    /v1/agent/conversations/{id}/summary   # AI 전달 요약
POST   /v1/agent/conversations/{id}/suggest   # 실시간 답변 추천

# 지식베이스
POST   /v1/kb                                 # 지식베이스 생성
POST   /v1/kb/{id}/documents                  # 문서 업로드 (비동기 인덱싱 job)
GET    /v1/kb/{id}/documents/{doc_id}/status
POST   /v1/kb/{id}/search                     # 검색 미리보기 (관리자 튜닝용)

# 관리
POST   /v1/admin/tenants                      # (SaaS 운영자) 테넌트 프로비저닝
GET    /v1/admin/analytics/overview           # 해결률·전환율·볼륨 대시보드
PUT    /v1/admin/prompts/{key}                # 프롬프트 버전 관리 (테넌트 오버라이드)
GET    /v1/admin/audit-logs
```

- 인증: `Authorization: Bearer <JWT>` (tenant_id claim 포함), 위젯은 사이트 키 + 세션 토큰 교환 방식
- 모든 응답 스트리밍은 SSE(`text/event-stream`) — 토큰 단위 delta + 완료 시 인용 출처 이벤트

## 4. MVP 범위 확정 (M1~M4)

**목표: "보험 FAQ/약관 문서를 올리면, 웹 위젯에서 근거 기반 AI 상담이 되고, 막히면 상담원에게 요약과 함께 넘어가는" 데모 가능한 제품**

### In (반드시)
- [ ] 테넌트/사용자/RBAC 기반 골격 + RLS
- [ ] 지식베이스: PDF/DOCX/텍스트 업로드 → 청킹·임베딩 → 하이브리드 검색
- [ ] 대화 파이프라인: 마스킹 → 의도분기(생성형/시나리오/전환) → RAG → 스트리밍 응답 + 출처
- [ ] LLM Gateway: anthropic + vllm 두 Provider 동작 (compose에서 vllm 프로파일 검증)
- [ ] 웹 채팅 위젯 + 상담원 핸드오프(대기열·수락·AI 요약 전달)
- [ ] 관리자 콘솔: 문서 관리, 상담 이력 조회, 기본 통계
- [ ] 감사 로그(pipeline_run), 개인정보 마스킹
- [ ] docker-compose 원커맨드 데모 환경

### Out (MVP 이후)
- 카카오 상담톡 연동(M5~), 시나리오 빌더 GUI(M5~), 과금 미터링 자동화(M7~)
- 음성(Phase 2), 아바타(Phase 3)
- 스키마 분리 Enterprise 테넌시, SSO 연동(온프렘 패키징 시점)

### 마일스톤 상세

| 월 | 목표 | 검증 기준 |
|----|------|-----------|
| M1 | 레포 골격, api-core 기동, 테넌트/인증, DB 마이그레이션 체계, compose 환경 | 테넌트 생성→로그인→헬스체크 E2E |
| M2 | 지식베이스 파이프라인 + LLM Gateway + 기본 대화(RAG 응답) | 문서 업로드→질문→출처 포함 답변 |
| M3 | 위젯, 핸드오프, 상담원 워크스페이스, 마스킹·감사 로그 | 위젯 상담→전환→상담원 수락 E2E |
| M4 | 관리자 콘솔·통계, vLLM 프로파일 검증, 부하 테스트, 데모 시나리오(보험 FAQ) | 사내 데모 + PoC 제안 가능 상태 |

## 5. 개발 규칙 (초기 합의)

- **API 계약 우선**: `libs/schemas`의 Pydantic 모델이 단일 진실 — OpenAPI 자동 생성, 프론트 타입 생성(openapi-typescript)
- **테스트**: 도메인 로직 단위 테스트 + 파이프라인 통합 테스트(LLM은 fake provider로 결정적 테스트), E2E는 compose 기반 smoke
- **마이그레이션**: Alembic, expand-contract(전방 호환) 원칙 — 온프렘 롤백 대비
- **환경 분기 금지 원칙**: `if is_onprem:` 식 분기 대신 Provider/설정 주입 (03 문서 §7 매트릭스 외 분기 금지)
- **프롬프트는 리소스**: `prompts/` 디렉토리에 버전 관리, 변경 시 회귀 평가셋(golden set) 통과 필수
- **AI 품질 평가**: 보험 도메인 Q&A 평가셋 100문항 구축(M2) → 근거 인용률·정확도 회귀 측정을 CI에 포함

## 6. 바로 시작할 첫 스프린트 백로그 (제안)

1. 모노레포 스캐폴딩(uv workspace + pnpm), Makefile, CI 파이프라인
2. api-core: FastAPI 기동, 설정 체계(pydantic-settings, 환경 프로파일), 헬스체크
3. DB 스키마 v0: tenant/user/conversation/message/kb/document/chunk/pipeline_run + Alembic + RLS
4. llm-gateway 라이브러리: `complete()`/`embed()` 인터페이스 + anthropic 구현 + fake 구현(테스트용)
5. docker-compose: postgres(pgvector)/redis/minio/api-core 기동
6. 인증: JWT 발급·검증, 테넌트 컨텍스트 미들웨어(RLS 세션 변수 설정)
