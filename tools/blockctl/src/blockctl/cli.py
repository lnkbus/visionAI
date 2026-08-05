"""``blockctl`` — 블록 카탈로그 CLI."""

from __future__ import annotations

from pathlib import Path

import typer

from blockctl.catalog import CatalogError, load_catalog, validate

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


if __name__ == "__main__":
    app()
