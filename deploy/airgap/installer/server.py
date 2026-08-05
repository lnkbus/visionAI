#!/usr/bin/env python3
"""설치 마법사 — 고객사 서버에서 브라우저로 설치한다.

**표준 라이브러리만 쓴다.** 폐쇄망 고객사 서버에는 pip도, 우리 이미지도 아직
없다. 마법사가 의존성을 요구하면 그 의존성부터 반입해야 하고, 그러면 마법사가
풀려던 문제를 마법사가 만든다. FastAPI를 쓰지 않는 이유가 이것이다.

설치 단계는 여기서 다시 구현하지 않고 ``install.sh``를 몰아준다. 두 번
구현하면 둘이 갈라지고, 갈라진 사실은 현장에서 한쪽만 실패할 때 드러난다.
스크립트가 ``VAI_PROGRESS=1``에서 내보내는 ``@@STEP|…`` 표시를 읽어 화면에
진행률로 옮긴다 — 산문을 파싱하면 문구를 고칠 때마다 화면이 조용히 깨진다.

    ./install.sh --gui              # 이 서버를 띄운다 (127.0.0.1:8099)

**127.0.0.1에만 바인딩한다.** 설치 마법사는 인증 없이 docker를 조작한다.
외부에 열면 그 자체가 원격 코드 실행 통로가 된다. 원격 설치가 필요하면
SSH 포트 포워딩을 쓴다 — 접근 통제를 우리가 새로 만들지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BIND_HOST = "127.0.0.1"

MARK = re.compile(r"^@@(STEP|OK|FAIL|DONE|ABORT)\|(.*)$")

# 블록 ID → 사람이 읽는 이름. 카탈로그(block.yaml)는 번들에 들어가지 않으므로
# 여기 둔다. 이름이 없으면 ID를 그대로 보여 준다 — 새 블록이 생겨도 화면이
# 비지 않게.
BLOCK_NAMES = {
    "AUD-RTP": "통화 오디오 수신 (RTP)",
    "AUD-VAD": "오디오 라우터 · 발화 구간 검출",
    "AVA-COUNSEL": "아바타 가상상담원",
    "BOT-VOICE": "음성봇 대화 엔진",
    "CORE-ADM": "운영 콘솔",
    "CORE-BUS": "세션 레지스트리 · 이벤트 버스",
    "CORE-GW": "실시간 게이트웨이",
    "CORE-LIC": "라이선스",
    "CORE-SEC": "보안 · 감사 로그",
    "FLT-MICRO": "개인정보 마스킹 · 컴플라이언스",
    "LLM-GW": "LLM 게이트웨이",
    "LLM-SUM": "요약 · 회의록",
    "RAG-KB": "지식베이스 수집기",
    "RAG-SRCH": "지식 검색",
    "SCN-STUDIO": "저작 · 학습 콘솔",
    "SPK-DIA": "화자 분리",
    "STT-CORE": "음성인식",
    "TA-ASSIST": "상담 어시스트",
    "TTS-CORE": "음성 합성",
    "UI-AGENT": "상담원 워크스페이스",
    "UI-MEET": "스마트 회의록",
}


class BundleError(RuntimeError):
    """번들이 온전하지 않다."""


def read_bundle(bundle: Path) -> dict[str, Any]:
    """번들이 무엇을 담고 있는지. 여기서 못 읽으면 설치를 시작하지 않는다."""
    plan_path = bundle / "plan.json"
    if not plan_path.is_file():
        raise BundleError(f"plan.json이 없다: {plan_path} — 번들이 온전하지 않다")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    version_path = bundle / "VERSION"
    images_tar = bundle / "images.tar"

    blocks = []
    required = set(plan.get("added_by_dependency") or [])
    for block_id in plan.get("blocks") or []:
        blocks.append(
            {
                "id": block_id,
                "name": BLOCK_NAMES.get(block_id, block_id),
                # 의존으로 끌려온 블록은 끌 수 없다. 끄면 켠 블록이 못 돈다.
                "required": block_id in required,
            }
        )

    return {
        "version": version_path.read_text(encoding="utf-8").strip()
        if version_path.is_file()
        else "(알 수 없음)",
        "blocks": blocks,
        "infra": plan.get("infra") or [],
        "image_count": len(plan.get("images") or []),
        "images_mb": round(images_tar.stat().st_size / 1048576) if images_tar.is_file() else 0,
        "warnings": plan.get("warnings") or [],
    }


def install_argv(bundle: Path, *, dry_run: bool, blocks: list[str], data_dir: str) -> list[str]:
    """실행할 명령. 화면에도 그대로 보여 준다 — 무엇이 돌지 모르는 설치는 무섭다."""
    argv = ["bash", str(bundle / "install.sh")]
    if dry_run:
        argv.append("--dry-run")
    if data_dir:
        argv += ["--data-dir", data_dir]
    if blocks:
        argv += ["--blocks", " ".join(blocks)]
    return argv


def stream_install(argv: list[str], out: queue.Queue[dict[str, Any]]) -> None:
    """설치를 돌리며 진행 표시와 원본 로그를 함께 흘린다.

    로그를 함께 보내는 이유는, 실패했을 때 필요한 것이 진행률이 아니라
    **docker가 뱉은 그 문장**이기 때문이다.
    """
    env = {**os.environ, "VAI_PROGRESS": "1"}
    try:
        proc = subprocess.Popen(  # argv는 이 파일에서만 구성한다 — 외부 입력이 섞이지 않는다
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        out.put({"kind": "ABORT", "message": f"설치 스크립트를 실행할 수 없다: {exc}"})
        out.put({"kind": "EOF"})
        return

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        found = MARK.match(line)
        if found:
            kind, payload = found.group(1), found.group(2)
            if kind == "STEP":
                index, total, name = payload.split("|", 2)
                out.put({"kind": "STEP", "index": int(index), "total": int(total), "name": name})
            else:
                out.put({"kind": kind, "message": payload})
            continue
        out.put({"kind": "LOG", "message": line})

    code = proc.wait()
    if code != 0:
        out.put({"kind": "ABORT", "message": f"설치 스크립트가 {code}로 끝났다"})
    out.put({"kind": "EOF"})


class Handler(BaseHTTPRequestHandler):
    bundle: Path = Path()
    server_version = "VisionAI-Installer"

    def log_message(self, fmt: str, *args: Any) -> None:
        # 기본 구현은 요청마다 stderr에 찍는다. 설치 중에는 설치 로그가
        # 보여야 하므로 접근 로그를 끈다.
        return

    # ── 응답 도우미 ────────────────────────────────────────────────────────

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    # ── 라우팅 ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # BaseHTTPRequestHandler 규약상 이 이름이어야 한다
        if self.path in ("/", "/index.html"):
            page = HERE / "index.html"
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        if self.path == "/api/bundle":
            try:
                self._json(200, read_bundle(self.bundle))
            except (BundleError, OSError, ValueError) as exc:
                self._json(500, {"error": str(exc)})
            return
        self._json(404, {"error": "없는 경로"})

    def do_POST(self) -> None:
        if self.path != "/api/install":
            self._json(404, {"error": "없는 경로"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "본문이 JSON이 아니다"})
            return

        blocks = [str(b) for b in (body.get("blocks") or [])]
        argv = install_argv(
            self.bundle,
            dry_run=bool(body.get("dry_run")),
            blocks=blocks,
            data_dir=str(body.get("data_dir") or ""),
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        events: queue.Queue[dict[str, Any]] = queue.Queue()
        # 명령을 먼저 보여 준다. 무엇이 돌지 모르는 설치는 승인할 수 없다.
        self._event({"kind": "CMD", "message": " ".join(shlex.quote(a) for a in argv)})

        worker = threading.Thread(target=stream_install, args=(argv, events), daemon=True)
        worker.start()

        while True:
            event = events.get()
            if event.get("kind") == "EOF":
                self._event({"kind": "EOF"})
                break
            try:
                self._event(event)
            except (BrokenPipeError, ConnectionResetError):
                # 브라우저가 닫혔다. 설치는 계속 돈다 — 중간에 끊으면 절반만
                # 적재된 상태가 남는다.
                break

    def _event(self, payload: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VisionAI 설치 마법사")
    parser.add_argument("--bundle", type=Path, default=HERE.parent, help="번들 디렉토리")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args(argv)

    bundle = args.bundle.resolve()
    try:
        info = read_bundle(bundle)
    except (BundleError, OSError, ValueError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    Handler.bundle = bundle
    httpd = ThreadingHTTPServer((BIND_HOST, args.port), Handler)

    print(f"VisionAI 설치 마법사 — 버전 {info['version']}")
    print(f"  블록 {len(info['blocks'])}개 · 이미지 {info['image_count']}개")
    print()
    print(f"  브라우저에서 열기:  http://{BIND_HOST}:{args.port}/")
    print()
    print("  원격 서버라면 SSH 포트 포워딩으로 연결한다 (외부에 열지 않는다):")
    print(f"    ssh -L {args.port}:127.0.0.1:{args.port} <사용자>@<서버>")
    print()
    print("  Ctrl+C 로 종료")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
