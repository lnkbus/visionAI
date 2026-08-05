"""``evalctl`` — 골든셋 평가 CLI.

evalctl run                 # 전체 스위트 실행, 사람이 읽는 리포트
evalctl run --suite pii -v  # 실패한 사례를 한 줄씩
evalctl baseline            # 현재 수치를 기준선으로 고정
evalctl compare             # 기준선 대비 회귀가 있으면 exit 1 (CI 게이트)
evalctl validate            # 골든셋 자체의 정합성 (정답 오타·커버리지)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import typer

from evalctl.baseline import (
    SuiteMetrics,
    blocking,
    compare,
    load_baseline,
    save_baseline,
)
from evalctl.dataset import (
    DatasetError,
    check_coverage,
    load_corpus,
    load_grounding,
    load_intent,
    load_pii,
    load_retrieval,
    load_tts,
)
from evalctl.metrics import CaseOutcome, CheckOutcome, summarize, summarize_checks
from evalctl.runners import (
    DEFAULT_TOP_K,
    corpus_path,
    run_grounding,
    run_intent,
    run_pii,
    run_retrieval,
    run_tts,
)

app = typer.Typer(help="VisionAI 골든셋 평가 하네스", no_args_is_help=True)

RETRIEVAL_FILE = "retrieval-terms.jsonl"
PII_FILE = "pii-masking.jsonl"
TTS_FILE = "tts-reading.jsonl"
GROUNDING_FILE = "answer-grounding.jsonl"
INTENT_FILE = "intent-routing.jsonl"
BASELINE_FILE = "baseline.json"

SUITE_NAMES = ("retrieval", "pii", "tts", "grounding", "intent")
"""``--suite all`` 이 도는 목록. 모델도 오디오도 없이 어디서나 돈다."""

OPTIONAL_SUITES = ("stt",)
"""명시적으로 지정해야 도는 스위트.

``all`` 에 넣지 않는 이유는 **모델 가중치와 오디오가 있어야** 돌기 때문이다.
섞어 두면 없는 환경에서 전체가 멈추고, 그러면 사람들은 곧 ``--suite`` 로
빼기 시작한다. 반대로 조용히 건너뛰게 하면 "STT 품질 통과"로 읽힌다."""


@dataclass
class SuiteResult:
    name: str
    metrics: dict[str, float]
    failures: list[str]
    """실패한 사례의 사람 읽는 설명. 리포트에서 이 줄만 보고 고칠 수 있어야 한다."""


def _eval_dir(root: Path | None) -> Path:
    base = (root or Path.cwd()) / "eval"
    if not base.is_dir():
        raise typer.BadParameter(f"eval 디렉토리를 찾을 수 없다: {base}")
    return base


def _retrieval_failures(outcomes: list[CaseOutcome]) -> list[str]:
    failures = []
    for outcome in outcomes:
        if outcome.hit and outcome.rank == 1:
            continue
        where = f"{outcome.rank}위" if outcome.hit else "미검출"
        failures.append(
            f"[{outcome.case_id}] {where} · 기대 {outcome.expected} · 실제 {outcome.returned}"
        )
    return failures


def _check_failures(outcomes: list[CheckOutcome]) -> list[str]:
    return [f"[{o.case_id}] {o.detail}" for o in outcomes if not o.passed]


async def _run_retrieval_suite(
    eval_dir: Path, top_k: int, expand: bool = True, tokenizer: str = "syllable"
) -> SuiteResult:
    cases, corpus_name = load_retrieval(eval_dir / RETRIEVAL_FILE)
    articles = load_corpus(corpus_path(eval_dir, corpus_name))
    outcomes = await run_retrieval(cases, articles, top_k=top_k, expand=expand, tokenizer=tokenizer)
    metrics = summarize(outcomes, top_k=top_k)
    return SuiteResult(
        name="retrieval",
        metrics={k: float(v) for k, v in metrics.to_dict().items()},
        failures=_retrieval_failures(outcomes),
    )


def _run_pii_suite(eval_dir: Path) -> SuiteResult:
    outcomes = run_pii(load_pii(eval_dir / PII_FILE))
    metrics = summarize_checks(outcomes)
    return SuiteResult(
        name="pii",
        metrics={k: float(v) for k, v in metrics.to_dict().items()},
        failures=_check_failures(outcomes),
    )


def _run_tts_suite(eval_dir: Path) -> SuiteResult:
    outcomes = run_tts(load_tts(eval_dir / TTS_FILE))
    metrics = summarize_checks(outcomes)
    return SuiteResult(
        name="tts",
        metrics={k: float(v) for k, v in metrics.to_dict().items()},
        failures=_check_failures(outcomes),
    )


def _run_grounding_suite(eval_dir: Path) -> SuiteResult:
    outcomes = run_grounding(load_grounding(eval_dir / GROUNDING_FILE))
    metrics = summarize_checks(outcomes)
    return SuiteResult(
        name="grounding",
        metrics={k: float(v) for k, v in metrics.to_dict().items()},
        failures=_check_failures(outcomes),
    )


async def _run_intent_suite(eval_dir: Path, use_classifier: bool = True) -> SuiteResult:
    outcomes = await run_intent(load_intent(eval_dir / INTENT_FILE), use_classifier=use_classifier)
    metrics = summarize_checks(outcomes)
    return SuiteResult(
        name="intent",
        metrics={
            **{k: float(v) for k, v in metrics.to_dict().items()},
            # 정확도와 따로 센다. 기권 두 건과 오라우팅 두 건은 같은 0.9가 아니다.
            "misroutes": float(sum(1 for o in outcomes if o.severity == "misroute")),
        },
        failures=_check_failures(outcomes),
    )


def run_suites(
    eval_dir: Path,
    suites: list[str],
    top_k: int,
    expand: bool = True,
    tokenizer: str = "syllable",
    stt_adapter: str = "fake",
    stt_model: str = "",
    stt_config: str = "",
) -> list[SuiteResult]:
    results: list[SuiteResult] = []
    if "retrieval" in suites:
        results.append(asyncio.run(_run_retrieval_suite(eval_dir, top_k, expand, tokenizer)))
    if "pii" in suites:
        results.append(_run_pii_suite(eval_dir))
    if "tts" in suites:
        results.append(_run_tts_suite(eval_dir))
    if "grounding" in suites:
        results.append(_run_grounding_suite(eval_dir))
    if "stt" in suites:
        results.append(_run_asr_suite(eval_dir, stt_adapter, stt_model, stt_config))
    if "intent" in suites:
        # --no-expand는 질의 확장과 어휘 분류기를 함께 끈다. 둘 다 "모델 없이
        # 도는 보강 계층"이라 한 스위치로 껐다 켜야 비교가 의미를 갖는다.
        results.append(asyncio.run(_run_intent_suite(eval_dir, expand)))
    return results


def _run_asr_suite(eval_dir: Path, adapter: str, model_path: str, config: str) -> SuiteResult:
    """음성인식 스위트.

    모델도 오디오도 있어야 돈다. 없으면 **왜 못 도는지 말하고 멈춘다** —
    조용히 건너뛰면 "STT 품질 통과"로 읽힌다.
    """
    from evalctl.asr import summarize_asr
    from evalctl.dataset import load_asr
    from evalctl.runners import AsrUnavailable, run_asr

    manifest = eval_dir / "asr-manifest.jsonl"
    cases, audio_root = load_asr(manifest)
    if not model_path:
        raise typer.BadParameter(
            "--stt-model 이 필요하다. 가중치는 "
            "`deploy/airgap/fetch_models.sh --out models --stt small` 로 받는다"
        )
    try:
        outcomes = asyncio.run(
            run_asr(
                cases,
                eval_dir / audio_root,
                adapter_name=adapter,
                model_path=model_path,
                config=json.loads(config) if config else {},
            )
        )
    except AsrUnavailable as exc:
        typer.secho(f"✗ 음성 스위트를 돌릴 수 없다: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    metrics = summarize_asr(outcomes)
    failures = [
        f"[{o.case_id}] 기대 {o.reference!r} ≠ 실제 {o.hypothesis!r}"
        for o in outcomes
        if o.char_errors
    ]
    return SuiteResult(name="stt", metrics=dict(metrics.to_dict()), failures=failures)


def _resolve_suites(suite: str) -> list[str]:
    if suite == "all":
        return list(SUITE_NAMES)
    if suite not in (*SUITE_NAMES, *OPTIONAL_SUITES):
        raise typer.BadParameter(
            f"알 수 없는 스위트: {suite} (가능: all, {', '.join((*SUITE_NAMES, *OPTIONAL_SUITES))})"
        )
    return [suite]


def _render(results: list[SuiteResult], verbose: bool) -> None:
    for result in results:
        metrics = result.metrics
        typer.secho(f"\n▸ {result.name}", bold=True)
        if result.name == "retrieval":
            typer.echo(
                f"  사례 {int(metrics['total'])}건 · "
                f"Recall@{int(metrics['top_k'])} {metrics['recall_at_k']:.3f} · "
                f"MRR {metrics['mrr']:.3f} · nDCG {metrics['ndcg_at_k']:.3f}"
            )
            typer.echo(
                f"  지연 평균 {metrics['mean_latency_ms']:.1f}ms · "
                f"p95 {metrics['p95_latency_ms']:.1f}ms (알고리즘 기준)"
            )
        elif result.name == "stt":
            typer.echo(
                f"  발화 {int(metrics['total'])}건 · "
                f"CER {metrics['cer']:.3f} · WER {metrics['wer']:.3f}"
            )
            typer.echo(
                f"  빈 인식 {int(metrics['empty'])}건 · p95 {metrics['p95_latency_ms']:.0f}ms"
            )
            # 한국어에서 기준선은 CER 로 잡는다. 띄어쓰기가 흔들려 WER 이
            # 실력과 무관하게 출렁이기 때문이다.
        else:
            known = int(metrics["known_limitations"])
            suffix = f" · 알려진 한계 {known}건" if known else ""
            if "misroutes" in metrics:
                suffix += f" · **오라우팅 {int(metrics['misroutes'])}건**"
            typer.echo(
                f"  사례 {int(metrics['total'])}건 · "
                f"정확도 {metrics['accuracy']:.3f} "
                f"({int(metrics['passed'])}/{int(metrics['total'])}){suffix}"
            )

        if result.failures:
            typer.secho(f"  실패 {len(result.failures)}건", fg=typer.colors.YELLOW)
            shown = result.failures if verbose else result.failures[:5]
            for line in shown:
                typer.echo(f"    ✗ {line}")
            if len(shown) < len(result.failures):
                typer.echo(f"    … 외 {len(result.failures) - len(shown)}건 (-v로 전체)")


def _as_suite_metrics(results: list[SuiteResult]) -> SuiteMetrics:
    return {result.name: result.metrics for result in results}


@app.command()
def run(
    root: Path = typer.Option(None, help="레포 루트"),
    suite: str = typer.Option(
        "all", help="all | retrieval | pii | tts | grounding | intent | stt(모델 필요)"
    ),
    top_k: int = typer.Option(DEFAULT_TOP_K, help="검색 상위 K (출하 설정과 같게 둔다)"),
    json_out: Path = typer.Option(None, "--json", help="결과를 JSON으로 저장"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="실패 사례를 전부 출력"),
    no_expand: bool = typer.Option(
        False,
        "--no-expand",
        help="모델 없는 보강 계층(질의 확장·어휘 의도 분류)을 끄고 돌린다 — 이득을 A/B로 확인할 때",
    ),
    tokenizer: str = typer.Option(
        "syllable", help="syllable(기본) | kiwi — 형태소 분석기 교체 이득을 재볼 때"
    ),
    stt_adapter: str = typer.Option("faster_whisper", help="음성 스위트 어댑터"),
    stt_model: str = typer.Option("", help="모델 경로 (--suite stt 에 필요)"),
    stt_config: str = typer.Option("", help="어댑터 설정 JSON"),
) -> None:
    """골든셋을 돌려 품질 수치를 낸다 (판정 없음)."""
    try:
        results = run_suites(
            _eval_dir(root),
            _resolve_suites(suite),
            top_k,
            not no_expand,
            tokenizer,
            stt_adapter,
            stt_model,
            stt_config,
        )
    except DatasetError as exc:
        typer.secho(f"골든셋 오류: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    _render(results, verbose)
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(_as_suite_metrics(results), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        typer.echo(f"\n결과 저장: {json_out}")


@app.command()
def baseline(
    root: Path = typer.Option(None, help="레포 루트"),
    top_k: int = typer.Option(DEFAULT_TOP_K),
    note: str = typer.Option("", help="왜 갱신했는지 — 나중에 diff만 보면 알 수 없다"),
) -> None:
    """현재 수치를 회귀 기준선으로 고정한다."""
    eval_dir = _eval_dir(root)
    try:
        results = run_suites(eval_dir, list(SUITE_NAMES), top_k)
    except DatasetError as exc:
        typer.secho(f"골든셋 오류: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    _render(results, verbose=False)
    target = eval_dir / BASELINE_FILE
    save_baseline(target, _as_suite_metrics(results), note=note)
    typer.secho(f"\n✓ 기준선 갱신: {target}", fg=typer.colors.GREEN)
    if not note:
        typer.secho(
            "  --note 없이 갱신했다. 다음 사람은 이 수치가 개선인지 후퇴인지 알 수 없다.",
            fg=typer.colors.YELLOW,
        )


@app.command("compare")
def compare_with_baseline(
    root: Path = typer.Option(None, help="레포 루트"),
    top_k: int = typer.Option(DEFAULT_TOP_K),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """기준선과 비교한다. 품질이 내려갔으면 exit 1 (CI 게이트)."""
    eval_dir = _eval_dir(root)
    try:
        results = run_suites(eval_dir, list(SUITE_NAMES), top_k)
    except DatasetError as exc:
        typer.secho(f"골든셋 오류: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    _render(results, verbose)

    base = load_baseline(eval_dir / BASELINE_FILE)
    if not base:
        typer.secho(
            "\n기준선이 없다 — `evalctl baseline`으로 먼저 고정한다.", fg=typer.colors.YELLOW
        )
        raise typer.Exit(1)

    findings = compare(_as_suite_metrics(results), base)
    typer.echo("")
    for finding in findings:
        mark = "✗" if finding.blocking else "△"
        color = typer.colors.RED if finding.blocking else typer.colors.YELLOW
        typer.secho(f"{mark} [{finding.suite}] {finding.metric}: {finding.message}", fg=color)

    if blocking(findings):
        typer.secho(f"\n회귀 {len(blocking(findings))}건 — 실패", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("✓ 회귀 없음", fg=typer.colors.GREEN)


@app.command()
def validate(root: Path = typer.Option(None, help="레포 루트")) -> None:
    """골든셋 자체를 검증한다 — 정답 오타, 코퍼스 커버리지 공백."""
    eval_dir = _eval_dir(root)
    try:
        cases, corpus_name = load_retrieval(eval_dir / RETRIEVAL_FILE)
        articles = load_corpus(corpus_path(eval_dir, corpus_name))
        pii_cases = load_pii(eval_dir / PII_FILE)
        tts_cases = load_tts(eval_dir / TTS_FILE)
        grounding_cases = load_grounding(eval_dir / GROUNDING_FILE)
        intent_cases = load_intent(eval_dir / INTENT_FILE)
    except DatasetError as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    problems = check_coverage(cases, articles)
    broken = [p for p in problems if "코퍼스에 없는 정답" in p]
    gaps = [p for p in problems if p not in broken]

    typer.echo(
        f"검색 {len(cases)}건 / 조항 {len(articles)}개 · "
        f"PII {len(pii_cases)}건 · TTS {len(tts_cases)}건 · "
        f"근거검증 {len(grounding_cases)}건 · 의도 {len(intent_cases)}건"
    )
    for gap in gaps:
        typer.secho(f"△ {gap}", fg=typer.colors.YELLOW)
    for problem in broken:
        typer.secho(f"✗ {problem}", fg=typer.colors.RED)
    if broken:
        raise typer.Exit(1)
    typer.secho("✓ 골든셋 정합성 통과", fg=typer.colors.GREEN)
