.PHONY: help install lint fmt type test check blocks up down logs demo

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

check: lint type test blocks  ## CI가 도는 전부

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

chart:  ## Helm 차트 검증 (lint + 기본값·패키지 오버레이 렌더링)
	cd deploy/charts/visionai && helm lint . && \
		helm template visionai . > /dev/null && \
		helm template visionai . -f values-aicc.yaml > /dev/null && \
		helm template visionai . -f values-meeting.yaml > /dev/null && \
		echo "✓ 차트 검증 통과"
