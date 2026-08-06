.PHONY: help install lint fmt type test check blocks eval eval-gate chart freeze freeze-write up down logs demo snapshot history

help:  ## 사용 가능한 타깃
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'

install:  ## 워크스페이스 의존성 설치
	uv sync --all-packages

lint:  ## 정적 검사 (ruff + 블록 경계)
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports

fmt:  ## 자동 포맷
	uv run ruff check --fix .
	uv run ruff format .

type:  ## 타입 검사 (테스트 제외 — pyproject 참조)
	uv run mypy libs blocks tools

test:  ## 전체 테스트 (Redis·GPU 불필요)
	uv run pytest -q

blocks:  ## 블록 카탈로그 검증 + 개발 규모 집계
	uv run blockctl check --root .
	uv run blockctl list --root .
	uv run blockctl effort --root .

eval:  ## 골든셋 평가 (검색·PII·TTS) — 실패 사례까지
	uv run evalctl validate --root .
	uv run evalctl run --root . -v

eval-gate:  ## 기준선 대비 품질 회귀 판정 (회귀 시 exit 1)
	uv run evalctl compare --root . -v

# helm lint 는 "렌더링이 되는가"까지만 본다. 필드 이름을 오타 내도 통과하므로
# kubeconform 으로 쿠버네티스 스키마까지 대조한다. 오버레이는 **전부** 돌린다 —
# 하나만 빼도 그 패키지에서만 깨지는 값이 끝까지 안 잡힌다.
chart:  ## Helm 차트 렌더링 + 쿠버네티스 스키마 검증 (kubeconform 필요)
	@cd deploy/charts/visionai && helm lint . && \
	for o in "" "-f values-aicc.yaml" "-f values-meeting.yaml" "-f values-voicebot.yaml" "-f values-avatar.yaml"; do \
		echo "▸ $${o:-기본값}"; helm template visionai . $$o | kubeconform -strict -summary -kubernetes-version 1.29.0; \
	done

freeze:  ## 패키지 형상 대조 (GS인증·납품 검수). PKG=meeting 지정 가능
	@for p in $(or $(PKG),meeting aicc voicebot avatar); do uv run blockctl freeze $$p --root .; done

freeze-write:  ## 형상 명세 갱신 — 의도한 구성 변경일 때만
	@for p in $(or $(PKG),meeting aicc voicebot avatar); do uv run blockctl freeze $$p --root . --write; done

history:  ## 릴리스 시점 이력 조회 + 무결성 검사
	uv run blockctl history --root .

snapshot:  ## 현재 형상을 릴리스 시점으로 박제 (VERSION=0.2.0 필수, NOTE= 선택)
	@test -n "$(VERSION)" || (echo "VERSION=0.2.0 이 필요하다"; exit 1)
	uv run blockctl snapshot $(VERSION) --root . $(if $(NOTE),--note "$(NOTE)",) $(if $(WRITE),--write,)
	@test -n "$(WRITE)" || echo "\n실제로 기록하려면: make snapshot VERSION=$(VERSION) WRITE=1"

check: lint type test blocks eval-gate freeze history  ## CI가 도는 전부

up:  ## compose 데모 환경 기동
	docker compose -f deploy/compose/docker-compose.yml up --build -d

down:  ## compose 환경 정리
	docker compose -f deploy/compose/docker-compose.yml down -v

logs:  ## compose 로그 추적
	docker compose -f deploy/compose/docker-compose.yml logs -f

demo:  ## 화면 주소 안내 (up 이후)
	@echo "고객 데모(마이크→자막) : http://localhost:8080/demo"
	@echo "상담원 워크스페이스     : http://localhost:8091/workspace"
	@echo "저작·학습 콘솔          : http://localhost:8090/console"
	@echo "스마트 회의록           : http://localhost:8094/minutes"

bundle:  ## 에어갭 반입 번들 생성 (LICENSE=customer.lic 또는 BLOCKS="A B")
	@deploy/airgap/build_bundle.sh $(if $(LICENSE),--license $(LICENSE),) \
		$(foreach b,$(BLOCKS),--block $(b)) --out dist/

bundle-plan:  ## 반입 계획만 계산 (docker 없이 확인)
	uv run blockctl bundle-plan $(if $(LICENSE),--license $(LICENSE),) \
		$(foreach b,$(BLOCKS),--block $(b))

release:  ## 온프렘 릴리스 빌드 (PUBLIC_KEY=... 필수, OBFUSCATE=1 로 난독화)
	@test -n "$(PUBLIC_KEY)" || (echo "PUBLIC_KEY=경로 가 필요하다"; exit 1)
	deploy/release/build_release.sh --public-key $(PUBLIC_KEY) \
		--out $(or $(OUT),dist/release) $(if $(OBFUSCATE),--obfuscate,)

release-check:  ## 릴리스 트리 납품 전 검사
	uv run blockctl release-check $(if $(ROOT),--root $(ROOT),)
