"""번들 compose 를 **적재된 이미지로 고정**한다.

개발용 compose 는 소스에서 빌드한다(`build:`). 그 파일을 그대로 번들에 넣으면
폐쇄망에서 이런 일이 생긴다: 이미지를 다 적재해 놓고 `docker compose up` 이
빌드를 시도하다 **빌드 컨텍스트가 없어서** 죽는다.

    resolve : lstat /tmp/deploy: no such file or directory

증상이 "설치가 실패했다"이고 로그는 빌드 오류를 가리키는데, 폐쇄망 담당자가
그 문구를 보고 할 수 있는 일이 없다. 반입은 되돌리기 어려우므로 이 실패는
비싸다.

그래서 번들에 넣을 때 바꾼다:

* `build:` 를 지운다 — 번들에는 소스가 없다
* `image:` 를 적재한 태그로 못 박는다
* `pull_policy: never` — 없는 이미지를 밖에서 받아오려 시도하지 않는다.
  폐쇄망에서 그 시도는 타임아웃으로만 나타나고, 원인이 이미지 누락이라는
  사실은 로그에 안 적힌다

앵커(`*block`)는 풀어서 쓴다. 주석이 사라지는 대신 **파일 하나만 읽으면
무엇이 도는지 알 수 있다** — 반입 심사에서 그 편이 낫다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HEADER = """# 이 파일은 반입 번들용으로 생성됐다 (deploy/airgap/pin_compose.py).
#
# 개발용 원본과 다른 점:
#   - build: 를 지웠다 — 번들에는 소스가 없다
#   - image: 를 적재한 태그로 못 박았다
#   - pull_policy: never — 폐쇄망에서 밖으로 나가지 않는다
#
# 고칠 일이 있으면 원본(deploy/compose/docker-compose.yml)을 고치고 번들을
# 다시 만든다. 이 파일을 직접 고치면 다음 반입에서 되돌아간다.
"""


def service_image(service: str, images: list[str]) -> str | None:
    """이 서비스가 쓸 이미지. **번들에 실제로 든 것만** 고른다.

    태그로 맞추지 않고 저장소 이름으로 찾는다. 블록마다 자기 버전을 갖기
    때문에(카탈로그의 `version`), 번들 태그를 그대로 붙이면 대부분 어긋난다 —
    그러면 산 블록이 통째로 빠진 번들이 조용히 나간다.
    """
    prefix = f"visionai/{service}:"
    return next((image for image in images if image.startswith(prefix)), None)


def pin(document: dict, *, images: list[str]) -> tuple[dict, list[str]]:
    """고정한 문서와, 번들에 이미지가 없어 **빠진** 서비스 목록."""
    services = document.get("services") or {}
    dropped: list[str] = []

    for name, service in list(services.items()):
        if not isinstance(service, dict):
            continue
        if "image" in service and "build" not in service:
            # 인프라(redis, qdrant). 이미 태그가 박혀 있다 — 번들에 든 것만 남긴다.
            if service["image"] not in images:
                dropped.append(name)
                del services[name]
                continue
            service["pull_policy"] = "never"
            continue

        image = service_image(name, images)
        if image is None:
            # 이 번들에 안 든 블록이다. 남겨 두면 `docker compose up` 이
            # 통째로 실패하므로 뺀다 — 산 블록만 들어가는 것이 정상이다.
            dropped.append(name)
            del services[name]
            continue

        service.pop("build", None)
        service["image"] = image
        service["pull_policy"] = "never"

    # 없어진 서비스를 가리키는 depends_on 을 정리한다. 남겨 두면
    # "undefined service" 로 기동 전체가 거부된다.
    alive = set(services)
    for service in services.values():
        depends = service.get("depends_on")
        if isinstance(depends, dict):
            service["depends_on"] = {k: v for k, v in depends.items() if k in alive}
            if not service["depends_on"]:
                del service["depends_on"]
        elif isinstance(depends, list):
            service["depends_on"] = [k for k in depends if k in alive]
            if not service["depends_on"]:
                del service["depends_on"]

    # 앵커는 서비스에 이미 펼쳐졌다. 정의만 남으면 읽는 사람이 헷갈린다.
    document.pop("x-block", None)
    return document, dropped


def main() -> int:
    parser = argparse.ArgumentParser(description="번들 compose 고정")
    parser.add_argument("--compose", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    document = yaml.safe_load(args.compose.read_text(encoding="utf-8"))
    document, dropped = pin(document, images=list(plan.get("images") or []))

    body = yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120)
    args.out.write_text(HEADER + body, encoding="utf-8")

    if dropped:
        print(f"  · 번들에 없는 서비스 제외: {', '.join(sorted(dropped))}")
    print(f"  ✓ compose 고정: 서비스 {len(document.get('services') or {})}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
