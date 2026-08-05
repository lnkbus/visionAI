# 모든 Python 블록이 공유하는 이미지 정의.
#
# 블록마다 Dockerfile을 따로 두면 베이스 이미지·보안 패치·빌드 옵션이 블록 수만큼
# 갈라진다. 폐쇄망 반입 시 검토 대상이 그만큼 늘어나므로 하나로 유지하고,
# 어떤 블록을 담을지는 BLOCK 인자로 정한다.
#
#   docker build -f deploy/docker/Dockerfile --build-arg BLOCK=stt-core -t visionai/stt-core .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY libs/ libs/
COPY blocks/ blocks/
COPY tools/ tools/

ARG BLOCK
RUN test -n "$BLOCK" || (echo "BLOCK 빌드 인자가 필요하다 (예: stt-core)" && exit 1)

# 실제 엔진은 선택 의존성이다. 기본 이미지에 넣지 않는 이유는 무게다 —
# faster-whisper 하나가 이미지를 수백 MB 늘리고, 폐쇄망 반입은 크기로 걸린다.
#
# 다만 **넣지 않으면 fake 어댑터밖에 못 쓴다.** 그 사실이 기동 로그에만 남으면
# "설치는 됐는데 인식이 안 된다"로 나타난다. 실물 데모·품질 측정용 이미지는
# EXTRAS 로 명시해 만든다:
#
#   docker build --build-arg BLOCK=stt-core --build-arg EXTRAS=whisper ...
ARG EXTRAS=""
RUN if [ -n "$EXTRAS" ]; then       uv sync --frozen --no-dev --package "vai-${BLOCK}" --extra "$EXTRAS";     else       uv sync --frozen --no-dev --package "vai-${BLOCK}";     fi

# 이미지가 어떤 엔진을 담고 있는지 남긴다. 라벨은 docker inspect 로 읽히므로
# "이 이미지로 실제 인식이 되는가"를 기동하지 않고도 알 수 있다.
LABEL ai.visionai.extras="${EXTRAS}"

# 블록 이름(stt-core)과 파이썬 모듈명(vai_stt_core)의 변환을 빌드 시점에 끝내
# 런타임 셸에서 문자열을 다루지 않게 한다.
RUN printf 'vai_%s' "$(echo "$BLOCK" | tr '-' '_')" > /app/.block-module

# 비루트 실행 — 고객사 보안 심사의 기본 점검 항목이다.
RUN useradd --system --uid 10001 visionai && chown -R visionai /app
USER visionai

EXPOSE 8080
CMD ["sh", "-c", "exec python -m $(cat /app/.block-module).main"]
