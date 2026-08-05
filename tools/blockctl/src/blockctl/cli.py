"""``blockctl`` — 블록 카탈로그 CLI."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from blockctl.bundle import licensed_blocks, plan
from blockctl.catalog import CatalogError, load_catalog, validate
from blockctl.freeze import FreezeError, diff, freeze_package, render
from blockctl.history import (
    HISTORY_PATH,
    HistoryError,
    ReleaseEntry,
    load_history,
)
from blockctl.history import append as append_release
from blockctl.history import render as render_history
from blockctl.history import verify as verify_history
from blockctl.release import Severity, blocking, embed_public_key, verify_release

app = typer.Typer(help="VisionAI 블록 카탈로그 도구", no_args_is_help=True)


def _blocks_dir(root: Path | None) -> Path:
    base = root or Path.cwd()
    candidate = base / "blocks"
    if not candidate.is_dir():
        raise typer.BadParameter(f"blocks 디렉토리를 찾을 수 없다: {candidate}")
    return candidate


@app.command("list")
def list_blocks(root: Path = typer.Option(None, help="레포 루트")) -> None:
    """블록 목록과 청구 단위를 출력한다."""
    blocks = load_catalog(_blocks_dir(root))
    width = max((len(b.manifest.id) for b in blocks), default=8)
    for block in sorted(blocks, key=lambda b: (b.manifest.tier.value, b.manifest.id)):
        m = block.manifest
        unit = "패키지 포함" if m.billing.included_in_package else m.billing.capacity_unit.value
        typer.echo(f"{m.id:<{width}}  {m.tier.value:<12}  {m.billing.dev_effort_mm:>4} MM  {unit}")


@app.command()
def check(root: Path = typer.Option(None, help="레포 루트")) -> None:
    """매니페스트 스키마와 조립 정합성을 검증한다 (CI 게이트)."""
    try:
        blocks = load_catalog(_blocks_dir(root))
    except CatalogError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    problems = validate(blocks)
    if problems:
        for problem in problems:
            typer.secho(f"✗ {problem}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho(f"✓ 블록 {len(blocks)}개 검증 통과", fg=typer.colors.GREEN)


@app.command()
def effort(root: Path = typer.Option(None, help="레포 루트")) -> None:
    """블록별 개발 규모 합계 — 견적 초안용."""
    blocks = load_catalog(_blocks_dir(root))
    total = sum(b.manifest.billing.dev_effort_mm for b in blocks)
    by_tier: dict[str, float] = {}
    for block in blocks:
        tier = block.manifest.tier.value
        by_tier[tier] = by_tier.get(tier, 0.0) + block.manifest.billing.dev_effort_mm
    for tier, mm in sorted(by_tier.items()):
        typer.echo(f"{tier:<14} {mm:>5.1f} MM")
    typer.echo(f"{'합계':<14} {total:>5.1f} MM")


@app.command("bundle-plan")
def bundle_plan(
    root: Path = typer.Option(None, help="레포 루트"),
    license_file: Path = typer.Option(None, "--license", help="고객사 .lic — 여기서 블록을 읽는다"),
    block: list[str] = typer.Option(None, help="블록 ID 직접 지정 (반복). --license 대신 쓴다"),
    out: Path = typer.Option(None, help="계획 JSON 출력 경로"),
) -> None:
    """에어갭 반입 계획을 계산한다.

    무엇을 담을지 사람이 목록으로 관리하면 반드시 하나가 빠지고, 그 사실은
    반입 승인이 끝난 고객사 현장에서야 드러난다. 라이선스와 카탈로그에서 계산한다.
    """
    blocks = load_catalog(_blocks_dir(root))

    requested = list(block or [])
    if license_file is not None:
        try:
            requested.extend(licensed_blocks(license_file))
        except (OSError, ValueError) as exc:
            typer.secho(f"✗ 라이선스를 읽을 수 없다: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
    if not requested:
        typer.secho("✗ --license 또는 --block이 필요하다", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    try:
        result = plan(blocks, sorted(set(requested)))
    except CatalogError as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    typer.echo(f"블록 {len(result.blocks)}개, 이미지 {len(result.images)}개")
    if result.added_by_dependency:
        # 조용히 늘어나면 반입 승인 목록과 실제가 어긋난다. 반드시 보여 준다.
        typer.secho(
            f"  의존으로 추가됨: {', '.join(result.added_by_dependency)}", fg=typer.colors.YELLOW
        )
    if result.added_by_recommendation:
        # 라이선스 범위를 넘는 반입이다. 영업·계약과 대조할 수 있게 따로 보여 준다.
        extra = ", ".join(result.added_by_recommendation)
        typer.secho(f"  기능 의존으로 추가됨(라이선스 범위 밖): {extra}", fg=typer.colors.YELLOW)
    for image in result.images:
        typer.echo(f"  {image}")
    for warning in result.warnings:
        typer.secho(f"  ! {warning}", fg=typer.colors.YELLOW)


@app.command("embed-key")
def embed_key(
    public_key: Path = typer.Option(..., "--public-key", help="발급 키쌍의 **공개**키 PEM"),
    root: Path = typer.Option(None, help="릴리스 트리 (기본: 현재 디렉토리)"),
) -> None:
    """공개키를 릴리스 트리에 심는다 — 이 단계를 거쳐야 서명 검증이 켜진다.

    개발 체크아웃과 릴리스 빌드의 차이는 딱 이것 하나다. 빠지면 서명 검증이
    꺼진 채로 나가고, 그 상태는 겉으로 완전히 정상으로 보인다.
    """
    base = root or Path.cwd()
    try:
        target = embed_public_key(base, public_key)
    except (OSError, ValueError) as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.secho(f"✓ 공개키 내장: {target.relative_to(base)}", fg=typer.colors.GREEN)


@app.command("release-check")
def release_check(root: Path = typer.Option(None, help="검사할 릴리스 트리")) -> None:
    """납품 직전 검사 — 서명 검증 활성화·개인키 유출·발급기 혼입 (CI/릴리스 게이트).

    사람이 눈으로 확인할 수 없는 것들만 모았다. 공개키가 빠진 빌드는 정상으로
    보이고, 개인키가 새면 되돌릴 수 없다.
    """
    findings = verify_release(root or Path.cwd())
    for finding in findings:
        color = typer.colors.RED if finding.severity is Severity.BLOCK else typer.colors.YELLOW
        typer.secho(str(finding), fg=color, err=finding.severity is Severity.BLOCK)

    fatal = blocking(findings)
    if fatal:
        typer.secho(f"\n✗ 납품 불가 — 차단 항목 {len(fatal)}건", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho("✓ 릴리스 검사 통과", fg=typer.colors.GREEN)


FREEZE_DIR = Path("deploy/release/packages")


@app.command()
def freeze(
    package: str = typer.Argument(..., help="패키지 이름 (values-<이름>.yaml)"),
    root: Path = typer.Option(None, help="레포 루트"),
    write: bool = typer.Option(False, "--write", help="형상 명세 파일을 갱신한다"),
) -> None:
    """패키지 형상을 계산해 기록본과 대조한다 (GS인증·납품 검수 게이트).

    기본 동작은 **검사**다. 구성이 바뀌었는데 명세가 그대로면 실패하고,
    무엇이 달라졌는지 항목 단위로 알려 준다. 의도한 변경이면 ``--write``로
    갱신하고 그 diff를 검토한다 — 형상 변경이 리뷰를 거치게 하는 것이 목적이다.
    """
    base = root or Path.cwd()
    chart_dir = base / "deploy" / "charts" / "visionai"
    try:
        blocks = load_catalog(_blocks_dir(root))
        current = freeze_package(package, blocks, root=base, chart_dir=chart_dir)
    except (CatalogError, FreezeError) as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    target = base / FREEZE_DIR / f"{package}.yaml"
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(current), encoding="utf-8")
        typer.secho(f"✓ 형상 명세 기록: {target.relative_to(base)}", fg=typer.colors.GREEN)
        typer.echo(
            f"  구성요소 {len(current.components)}개 · 해시 {current.configuration_hash[:16]}"
        )
        return

    if not target.is_file():
        typer.secho(
            f"✗ 형상 명세가 없다: {target.relative_to(base)} — `blockctl freeze {package} --write`",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    recorded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    problems = diff(current, recorded)
    if problems:
        typer.secho(f"✗ 형상이 기록본과 다르다 ({package})", fg=typer.colors.RED, err=True)
        for problem in problems:
            typer.secho(f"  · {problem}", fg=typer.colors.RED, err=True)
        typer.echo(f"\n의도한 변경이면: blockctl freeze {package} --write")
        raise typer.Exit(1)
    typer.secho(
        f"✓ 형상 일치 ({package}) — 구성요소 {len(current.components)}개 · "
        f"해시 {current.configuration_hash[:16]}",
        fg=typer.colors.GREEN,
    )


PACKAGES = ("meeting", "aicc", "voicebot", "avatar")


def _head_commit(root: Path) -> str:
    """되돌아갈 좌표. 여기서만 깃을 부른다 — 나머지 계산은 순수 함수다."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise typer.BadParameter("깃 커밋을 읽을 수 없다 — --commit으로 직접 준다") from exc
    return out.stdout.strip()


@app.command()
def snapshot(
    version: str = typer.Argument(..., help="릴리스 버전 (0.3.0)"),
    root: Path = typer.Option(None, help="레포 루트"),
    note: str = typer.Option("", help="이 시점을 한 줄로"),
    commit: str = typer.Option("", help="깃 커밋 (기본: HEAD)"),
    date: str = typer.Option("", help="릴리스 날짜 YYYY-MM-DD (기본: 오늘 UTC)"),
    write: bool = typer.Option(False, "--write", help="이력에 실제로 추가한다"),
) -> None:
    """현재 형상을 하나의 릴리스 시점으로 박제한다.

    "3월에 저 고객사가 받은 게 정확히 무엇인가"에 답하기 위한 기록이다.
    버전 문자열만으로는 부족하다 — 같은 버전을 표방하는 서로 다른 물건이
    생길 수 있고, 그것이 형상 관리가 막으려는 상황이다.

    기본 동작은 **미리보기**다. ``--write``를 줘야 이력에 들어가고, 들어간
    뒤에는 고치지 않는다. 같은 커밋에 ``git tag v<버전>``을 붙인다.
    """
    base = root or Path.cwd()
    chart_dir = base / "deploy" / "charts" / "visionai"
    try:
        blocks = load_catalog(_blocks_dir(root))
        freezes = {
            name: freeze_package(name, blocks, root=base, chart_dir=chart_dir) for name in PACKAGES
        }
    except (CatalogError, FreezeError) as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    # 명세와 어긋난 상태를 박제하면 이력이 거짓이 된다. 여기서 먼저 막는다.
    drifted = [
        name
        for name, current in freezes.items()
        if diff(current, yaml.safe_load((base / FREEZE_DIR / f"{name}.yaml").read_text("utf-8")))
        if (base / FREEZE_DIR / f"{name}.yaml").is_file()
    ]
    if drifted:
        typer.secho(
            f"✗ 형상 명세와 어긋난 패키지가 있다: {', '.join(drifted)} — "
            "`blockctl freeze <패키지> --write`로 맞춘 뒤 박제한다",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    entry = ReleaseEntry(
        version=version,
        released_at=date or datetime.now(UTC).strftime("%Y-%m-%d"),
        commit=commit or _head_commit(base),
        packages={name: fz.configuration_hash for name, fz in freezes.items()},
        blocks={b.manifest.id: b.manifest.version for b in blocks},
        note=note,
    )

    target = base / HISTORY_PATH
    try:
        entries = append_release(load_history(target), entry)
    except HistoryError as exc:
        typer.secho(f"✗ 이력에 추가할 수 없다:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"버전   {entry.version}  ({entry.released_at})")
    typer.echo(f"커밋   {entry.commit}")
    for name, digest in sorted(entry.packages.items()):
        typer.echo(f"  {name:<9} {digest[:16]}")

    if not write:
        typer.secho("\n미리보기다. 이력에 넣으려면 --write", fg=typer.colors.YELLOW)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_history(entries), encoding="utf-8")
    typer.secho(f"✓ 시점 기록: {HISTORY_PATH}", fg=typer.colors.GREEN)
    typer.echo(f"  같은 커밋에 태그를 붙인다: git tag v{entry.version} {entry.commit[:12]}")


@app.command("history")
def history_cmd(root: Path = typer.Option(None, help="레포 루트")) -> None:
    """릴리스 시점 이력을 출력하고 무결성을 검사한다 (CI 게이트)."""
    base = root or Path.cwd()
    try:
        entries = load_history(base / HISTORY_PATH)
    except HistoryError as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    problems = verify_history(entries)
    if problems:
        typer.secho("✗ 이력 무결성 위반", fg=typer.colors.RED, err=True)
        for problem in problems:
            typer.secho(f"  · {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if not entries:
        typer.secho(
            "기록된 릴리스 시점이 없다 — `blockctl snapshot <버전> --write`", fg=typer.colors.YELLOW
        )
        return

    for entry in entries:
        head = f"{entry.version:<10} {entry.released_at}  {entry.commit[:12]}"
        typer.echo(f"{head}  {entry.note}" if entry.note else head)
    typer.secho(f"✓ 시점 {len(entries)}건 무결성 통과", fg=typer.colors.GREEN)


@app.command()
def capacity(
    seats: int = typer.Argument(..., help="상담석 수"),
    concurrency: float = typer.Option(0.75, help="동시 통화율(피크 기준)"),
    stt: str = typer.Option("stt-small-gpu", help="STT 프로파일"),
    tts: bool = typer.Option(False, "--tts", help="음성봇(TTS)을 포함해 계산"),
    no_diarization: bool = typer.Option(False, "--no-diarization", help="화자분리 제외"),
    json_out: bool = typer.Option(False, "--json", help="JSON 출력"),
) -> None:
    """상담석 수에서 동시 채널과 GPU를 계산한다 (견적·라이선스).

    **상담석 하나가 채널 하나가 아니다.** 상담사와 고객이 동시에 말하므로
    2채널이 필요하다. 1,000석 = 2,000채널이며, 1,000으로 잡으면 도입 직후
    절반이 인식되지 않는다.
    """
    from blockctl.capacity import CHANNELS_PER_SEAT, PROFILES, plan_capacity

    try:
        result = plan_capacity(
            seats,
            concurrency=concurrency,
            stt_profile=stt,
            with_tts=tts,
            with_diarization=not no_diarization,
        )
    except ValueError as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if json_out:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    typer.secho(f"\n상담석 {result.seats:,}석", bold=True)
    typer.echo(
        f"  x {CHANNELS_PER_SEAT}채널(상담사+고객) x 동시 {result.concurrency:.0%} "
        f"= **동시 {result.channels:,}채널**"
    )
    typer.echo()
    width = max(len(r.engine) for r in result.requirements)
    for requirement in result.requirements:
        typer.echo(
            f"  {requirement.engine:<{width}}  {requirement.channels:>6,}채널  "
            f"GPU {requirement.gpus:>3}장   {requirement.note}"
        )
    typer.echo()
    typer.echo(f"  라이선스 동시 채널: {result.channels:,}")
    for warning in result.warnings:
        typer.secho(f"  ⚠ {warning}", fg=typer.colors.YELLOW)
    typer.echo()
    typer.echo(f"  프로파일: {', '.join(sorted(PROFILES))}")


if __name__ == "__main__":
    app()
