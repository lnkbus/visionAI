"""``licgen`` — 발급 서버에서 라이선스를 만드는 CLI.

전형적인 흐름::

    # 최초 1회 (키쌍 생성 — 개인키는 이 서버 밖으로 나가지 않는다)
    licgen keygen --out-dir /secure/keys

    # 고객사가 반출한 요청서로 발급
    licgen issue customer.req \\
        --key /secure/keys/license_key.pem \\
        --expires 2027-12-31 \\
        --block STT-CORE:concurrent_channels=50 \\
        --block RAG-SRCH --block LLM-GW \\
        --catalog-root . \\
        --out customer.lic

    # 반출 전 확인 (제품과 같은 검증기로)
    licgen inspect customer.lic --public-key /secure/keys/license_pub.pem
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import typer

from licgen.issuer import IssueError, catalog_block_ids, generate_key_pair, issue, parse_grant
from vai_common.license import LicenseError, LicenseGate
from vai_contracts.licensing import LicenseRequest

app = typer.Typer(help="VisionAI 라이선스 발급기 (공급사 전용)", no_args_is_help=True)

PRIVATE_KEY_NAME = "license_key.pem"
PUBLIC_KEY_NAME = "license_pub.pem"


def _fail(message: str) -> typer.Exit:
    typer.secho(f"✗ {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


@app.command()
def keygen(
    out_dir: Path = typer.Option(..., "--out-dir", help="키를 저장할 디렉토리"),
    bits: int = typer.Option(4096, help="RSA 키 길이"),
    force: bool = typer.Option(False, help="기존 키를 덮어쓴다"),
) -> None:
    """발급용 RSA 키쌍을 만든다.

    **개인키를 덮어쓰면 이미 나간 라이선스를 다시 발급할 수 없다.** 그래서
    ``--force`` 없이는 덮어쓰지 않는다.
    """
    private_path = out_dir / PRIVATE_KEY_NAME
    public_path = out_dir / PUBLIC_KEY_NAME
    if not force and (private_path.exists() or public_path.exists()):
        raise _fail(f"이미 키가 있다: {out_dir} (덮어쓰려면 --force)")

    try:
        private_pem, public_pem = generate_key_pair(bits)
    except IssueError as exc:
        raise _fail(str(exc)) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    # 개인키는 소유자만 읽을 수 있어야 한다. 쓰고 나서 chmod 하면 그 사이에
    # 읽힐 수 있으므로 0600으로 열어서 쓴다.
    with os.fdopen(os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(private_pem)
    public_path.write_bytes(public_pem)

    typer.secho(
        f"✓ 개인키: {private_path} (0600 — 이 서버 밖으로 반출 금지)", fg=typer.colors.GREEN
    )
    typer.secho(f"✓ 공개키: {public_path}", fg=typer.colors.GREEN)
    typer.echo(
        "  릴리스 빌드가 공개키를 libs/common/src/vai_common/keys/license_pub.pem 으로 심는다."
    )


@app.command("issue")
def issue_license(
    request_file: Path = typer.Argument(..., help="설치 서버가 만든 발급요청서(.req)"),
    key: Path = typer.Option(..., help="발급용 개인키 PEM"),
    out: Path = typer.Option(..., help="출력할 .lic 경로"),
    expires: str = typer.Option(
        "", help="만료일 YYYY-MM-DD (생략하면 무기한 — 영구 라이선스에만 쓴다)"
    ),
    block: list[str] = typer.Option(None, help="허가 블록. 'ID' 또는 'ID:unit=N' (반복 지정)"),
    from_request: bool = typer.Option(False, help="요청서의 requested_blocks를 그대로 허가한다"),
    catalog_root: Path = typer.Option(
        None, help="레포 루트. 주면 블록 ID 오타를 카탈로그와 대조해 막는다"
    ),
    key_id: str = typer.Option("default", help="서명 키 식별자 — 키 교체 이력 추적용"),
) -> None:
    """요청서에 서명해 ``.lic``을 만든다."""
    try:
        request = LicenseRequest.model_validate_json(request_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _fail(f"요청서를 읽을 수 없다: {exc}") from exc

    specs = list(block or [])
    if from_request:
        specs.extend(request.requested_blocks)
    if not specs:
        raise _fail("허가할 블록이 없다 (--block 또는 --from-request)")

    grants: dict[str, dict[str, int]] = {}
    try:
        for spec in specs:
            block_id, capacity = parse_grant(spec)
            grants.setdefault(block_id, {}).update(capacity)
    except IssueError as exc:
        raise _fail(str(exc)) from exc

    if catalog_root is not None:
        known = catalog_block_ids(catalog_root)
        unknown = sorted(set(grants) - known)
        if unknown:
            # 오타 하나로 고객사의 블록이 기동하지 못한다. 발급 시점에 막는다.
            raise _fail(f"카탈로그에 없는 블록: {', '.join(unknown)}")

    try:
        expires_on = date.fromisoformat(expires) if expires else None
    except ValueError as exc:
        raise _fail(f"만료일 형식이 잘못됐다: '{expires}' (YYYY-MM-DD)") from exc

    try:
        issued = issue(
            request,
            private_key_pem=key.read_bytes(),
            expires=expires_on,
            grants=grants,
            issued_on=datetime.now(UTC).date(),
            key_id=key_id,
        )
    except (IssueError, LicenseError, OSError) as exc:
        raise _fail(str(exc)) from exc

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(issued.dumps(), encoding="utf-8")
    typer.secho(f"✓ 발급 완료: {out}", fg=typer.colors.GREEN)
    typer.echo(f"  고객사: {request.customer_id}{f' / {request.site}' if request.site else ''}")
    typer.echo(f"  만료:   {expires_on or '무기한'}")
    typer.echo(f"  블록:   {', '.join(sorted(grants))}")


@app.command()
def inspect(
    license_file: Path = typer.Argument(..., help="검사할 .lic"),
    public_key: Path = typer.Option(None, help="공개키 PEM. 주면 서명을 실제로 검증한다"),
) -> None:
    """라이선스 내용을 확인한다 — 반출 전 최종 점검.

    제품과 **같은** 검증기(:class:`LicenseGate`)를 쓴다. 발급기 전용 검사기를
    따로 두면 통과했는데 제품에서 거부되는 일이 생긴다.

    지문 검증은 하지 않는다 — 발급 서버는 고객사 서버가 아니므로 항상 어긋난다.
    """
    try:
        document = json.loads(license_file.read_text(encoding="utf-8"))
        gate = LicenseGate.from_document(
            document,
            public_key_pem=public_key.read_bytes() if public_key else None,
            verify_fingerprint=False,
        )
    except (LicenseError, OSError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    status = gate.status()
    typer.echo(f"고객사   : {status.customer_id}")
    typer.echo(f"만료     : {status.expires or '무기한'}")
    typer.echo(f"서명검증 : {'✓ 통과' if status.signature_verified else '(공개키 미지정)'}")
    typer.echo(f"블록     : {', '.join(status.blocks)}")
    for block_id, capacity in sorted(status.capacity.items()):
        limits = ", ".join(f"{unit}={limit}" for unit, limit in sorted(capacity.items()))
        typer.echo(f"  {block_id}: {limits}")
    if status.expires and gate.is_expired():
        typer.secho("✗ 이미 만료된 라이선스다", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
