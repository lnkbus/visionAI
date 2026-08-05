"""``blockctl`` — 블록 카탈로그 CLI."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from blockctl.bundle import licensed_blocks, plan
from blockctl.catalog import CatalogError, load_catalog, validate
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


if __name__ == "__main__":
    app()
