#!/usr/bin/env python3
"""Consolidate and analyze the three formal SBIMS cold-start k6 trials.

Reads raw k6 NDJSON files and writes reproducible CSV tables, figures, and a
Markdown report. Authentication-preparation requests are excluded from SBIMS
endpoint latency calculations.

Default inputs (relative to the project root):
  results/input/trial-01.json
  results/input/trial-02.json
  results/input/trial-03.json

Default outputs:
  results/analysis/request_level.csv
  results/analysis/cycle_summary.csv
  results/analysis/trial_summary.csv
  results/analysis/overall_summary.csv
  results/analysis/initialization_summary.csv
  results/analysis/failure_analysis.csv
  results/analysis/figure_01_paired_cycle_comparison.png
  results/analysis/figure_02_latency_distribution.png
  results/analysis/figure_03_trial_mean_comparison.png
  results/analysis/analysis_report.md

Requirements: Python 3.10+, matplotlib. No pandas dependency is required.
"""  # noqa: EXE001

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ENDPOINT_TAG = "sbims_students_me"
HTTP_METRICS = {
    "http_req_duration": "http_req_duration_ms",
    "http_req_waiting": "http_req_waiting_ms",
    "http_req_connecting": "http_req_connecting_ms",
    "http_req_tls_handshaking": "http_req_tls_handshaking_ms",
    "http_req_failed": "http_req_failed",
}
INIT_METRICS = {
    "sbims_application_initialization_age_ms": "application_initialization_age_ms",
    "sbims_initialization_headers_present": "initialization_headers_present",
}
CYCLE_METRICS = {
    "sbims_controlled_wait_ms": "controlled_wait_ms",
    "sbims_idle_gap_ms": "elapsed_since_previous_sbims_request_ms",
}


def as_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def sd(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) > 1 else (0.0 if values else None)


def stats(prefix: str, values: list[float]) -> dict[str, Any]:
    return {
        f"{prefix}_count": len(values),
        f"{prefix}_mean_ms": mean(values),
        f"{prefix}_median_ms": median(values),
        f"{prefix}_sd_ms": sd(values),
        f"{prefix}_min_ms": min(values) if values else None,
        f"{prefix}_max_ms": max(values) if values else None,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # Stable union of columns, preserving first-seen order.
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def request_key(tags: dict[str, Any], default_trial: str) -> tuple[str, int, str, int]:
    trial = str(tags.get("trial_id") or default_trial)
    cycle = int(str(tags.get("cycle_id") or "0"))
    klass = str(tags.get("request_class") or "")
    sequence = int(str(tags.get("request_sequence") or "0"))
    return trial, cycle, klass, sequence


def load_trial(
    path: Path,
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], dict[str, float]]]:
    """Merge k6 metric points into one record per SBIMS endpoint request."""
    default_trial = path.stem
    requests: dict[tuple[str, int, str, int], dict[str, Any]] = {}
    cycle_values: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                point = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {path}, line {line_no}: {exc}"
                ) from exc

            metric = point.get("metric")
            data = point.get("data") or {}
            tags = data.get("tags") or {}
            value = as_float(data.get("value"))

            if metric in CYCLE_METRICS:
                trial = str(tags.get("trial_id") or default_trial)
                cycle = int(str(tags.get("cycle_id") or "0"))
                if cycle and value is not None:
                    cycle_values[(trial, cycle)][CYCLE_METRICS[metric]] = value
                continue

            if metric not in HTTP_METRICS and metric not in INIT_METRICS:
                continue
            if tags.get("name") != ENDPOINT_TAG:
                # Excludes Supabase Auth token preparation and unrelated traffic.
                continue
            klass = str(tags.get("request_class") or "")
            if klass not in {"first", "warm"}:
                continue

            key = request_key(tags, default_trial)
            trial, cycle, request_class, sequence = key
            if cycle < 1 or sequence < 1:
                raise ValueError(
                    f"Missing/invalid cycle or request sequence in {path}: {tags}"
                )

            row = requests.setdefault(
                key,
                {
                    "trial_id": trial,
                    "cycle_id": cycle,
                    "request_class": request_class,
                    "request_sequence": sequence,
                    "timestamp": data.get("time", ""),
                    "http_status": str(tags.get("status") or ""),
                    "source_file": path.name,
                },
            )
            if metric in HTTP_METRICS:
                row[HTTP_METRICS[metric]] = value
            elif metric in INIT_METRICS:
                row[INIT_METRICS[metric]] = value

    rows = list(requests.values())
    rows.sort(
        key=lambda r: (
            r["trial_id"],
            r["cycle_id"],
            0 if r["request_class"] == "first" else 1,
            r["request_sequence"],
        )
    )
    for row in rows:
        status = row.get("http_status", "")
        failed = row.get("http_req_failed")
        row["http_success"] = bool(
            status.isdigit() and 200 <= int(status) < 300 and failed == 0.0
        )
    return rows, cycle_values


def validate_trials(
    rows: list[dict[str, Any]],
    cycle_values: dict,
    expected_cycles: int,
    expected_warm: int,
    idle_seconds: int,
    wait_tolerance_ms: int,
    strict: bool,
) -> list[str]:
    issues: list[str] = []
    trials = sorted({r["trial_id"] for r in rows})
    for trial in trials:
        tr = [r for r in rows if r["trial_id"] == trial]
        cycles = sorted({r["cycle_id"] for r in tr})
        if len(cycles) != expected_cycles or cycles != list(
            range(1, expected_cycles + 1)
        ):
            issues.append(
                f"{trial}: expected cycles 1-{expected_cycles}; found {cycles}"
            )
        for cycle in range(1, expected_cycles + 1):
            cr = [r for r in tr if r["cycle_id"] == cycle]
            first = [r for r in cr if r["request_class"] == "first"]
            warm = [r for r in cr if r["request_class"] == "warm"]
            if len(first) != 1:
                issues.append(
                    f"{trial} cycle {cycle}: expected 1 first request; found {len(first)}"
                )
            if len(warm) != expected_warm:
                issues.append(
                    f"{trial} cycle {cycle}: expected {expected_warm} warm requests; found {len(warm)}"
                )
            seqs = sorted(r["request_sequence"] for r in warm)
            if seqs != list(range(1, expected_warm + 1)):
                issues.append(
                    f"{trial} cycle {cycle}: warm request sequences are {seqs}"
                )
            if (
                trial,
                cycle,
            ) not in cycle_values or "controlled_wait_ms" not in cycle_values[
                (trial, cycle)
            ]:
                issues.append(
                    f"{trial} cycle {cycle}: missing controlled-wait measurement"
                )
            else:
                actual_wait = cycle_values[(trial, cycle)]["controlled_wait_ms"]
                minimum_accepted = idle_seconds * 1000 - wait_tolerance_ms
                if actual_wait < minimum_accepted:
                    issues.append(
                        f"{trial} cycle {cycle}: controlled wait {actual_wait:.0f} ms is below the accepted minimum {minimum_accepted} ms"
                    )
    if issues and strict:
        raise ValueError("Input validation failed:\n- " + "\n- ".join(issues))
    return issues


def make_cycle_rows(
    rows: list[dict[str, Any]], cycle_values: dict
) -> list[dict[str, Any]]:
    out = []
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["trial_id"], row["cycle_id"])].append(row)
    for (trial, cycle), group in sorted(groups.items()):
        first_rows = [r for r in group if r["request_class"] == "first"]
        warm_rows = [r for r in group if r["request_class"] == "warm"]
        first = first_rows[0] if len(first_rows) == 1 else None
        warm_ok = [
            r
            for r in warm_rows
            if r["http_success"] and r.get("http_req_duration_ms") is not None
        ]
        first_ok = bool(
            first
            and first["http_success"]
            and first.get("http_req_duration_ms") is not None
        )
        warm_mean = mean([r["http_req_duration_ms"] for r in warm_ok])
        first_latency = first.get("http_req_duration_ms") if first else None
        paired = (
            first_ok
            and len(warm_ok) == len(warm_rows)
            and len(warm_rows) > 0
            and warm_mean is not None
        )
        diff = first_latency - warm_mean if paired else None
        pct = diff / warm_mean * 100 if diff is not None and warm_mean else None
        cm = cycle_values.get((trial, cycle), {})
        out.append(
            {
                "trial_id": trial,
                "cycle_id": cycle,
                "first_request_latency_ms": first_latency,
                "first_request_success": first["http_success"] if first else False,
                "warm_request_count": len(warm_rows),
                "warm_successful_count": len(warm_ok),
                "warm_average_latency_ms_success_only": warm_mean,
                "warm_median_latency_ms_success_only": median(
                    [r["http_req_duration_ms"] for r in warm_ok]
                ),
                "latency_difference_ms_paired_success_only": diff,
                "relative_increase_pct_paired_success_only": pct,
                "paired_comparison_valid": paired,
                "controlled_wait_ms": cm.get("controlled_wait_ms"),
                "elapsed_since_previous_sbims_request_ms": cm.get(
                    "elapsed_since_previous_sbims_request_ms"
                ),
                "first_initialization_age_ms": first.get(
                    "application_initialization_age_ms"
                )
                if first
                else None,
                "first_initialization_headers_present": first.get(
                    "initialization_headers_present"
                )
                if first
                else None,
            }
        )
    return out


def summarize_scope(
    scope: str, rows: list[dict[str, Any]], cycle_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    successful = [r for r in rows if r["http_success"]]
    failed = [r for r in rows if not r["http_success"]]
    result: dict[str, Any] = {
        "scope": scope,
        "request_count": len(rows),
        "successful_requests": len(successful),
        "failed_requests": len(failed),
        "success_rate_pct": (len(successful) / len(rows) * 100) if rows else None,
    }
    for klass in ("first", "warm"):
        all_k = [r for r in rows if r["request_class"] == klass]
        ok_k = [
            r
            for r in all_k
            if r["http_success"] and r.get("http_req_duration_ms") is not None
        ]
        lat = [r["http_req_duration_ms"] for r in ok_k]
        result[f"{klass}_requests_total"] = len(all_k)
        result[f"{klass}_requests_successful"] = len(ok_k)
        result[f"{klass}_requests_failed"] = len(all_k) - len(ok_k)
        result.update(stats(f"{klass}_latency", lat))
        for metric, suffix in (
            ("http_req_waiting_ms", "ttfb"),
            ("http_req_connecting_ms", "connecting"),
            ("http_req_tls_handshaking_ms", "tls_handshake"),
        ):
            vals = [r[metric] for r in ok_k if r.get(metric) is not None]
            result[f"{klass}_{suffix}_mean_ms"] = mean(vals)
    pairs = [c for c in cycle_rows if c["trial_id"] == scope or scope == "all_trials"]
    pct = [
        c["relative_increase_pct_paired_success_only"]
        for c in pairs
        if c.get("relative_increase_pct_paired_success_only") is not None
    ]
    diff = [
        c["latency_difference_ms_paired_success_only"]
        for c in pairs
        if c.get("latency_difference_ms_paired_success_only") is not None
    ]
    result["valid_paired_cycles"] = len(pct)
    result["first_slower_paired_cycles"] = sum(1 for x in diff if x > 0)
    result["mean_cycle_relative_increase_pct"] = mean(pct)
    result["median_cycle_relative_increase_pct"] = median(pct)
    result["mean_cycle_latency_difference_ms"] = mean(diff)
    waits = [
        r.get("controlled_wait_ms")
        for r in pairs
        if r.get("controlled_wait_ms") is not None
    ]
    result["controlled_wait_min_minutes"] = min(waits) / 60000 if waits else None
    result["controlled_wait_max_minutes"] = max(waits) / 60000 if waits else None
    return result


def initialization_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for klass in ("first", "warm"):
        subset = [r for r in rows if r["request_class"] == klass]
        ages = [
            r["application_initialization_age_ms"]
            for r in subset
            if r.get("application_initialization_age_ms") is not None
        ]
        headers = [
            r["initialization_headers_present"]
            for r in subset
            if r.get("initialization_headers_present") is not None
        ]
        out.append(
            {
                "request_class": klass,
                "observations": len(subset),
                "initialization_age_observations": len(ages),
                "initialization_age_mean_ms": mean(ages),
                "initialization_age_median_ms": median(ages),
                "initialization_age_sd_ms": sd(ages),
                "initialization_age_min_ms": min(ages) if ages else None,
                "initialization_age_max_ms": max(ages) if ages else None,
                "headers_present_count": sum(1 for x in headers if x == 1.0),
                "headers_absent_count": sum(1 for x in headers if x == 0.0),
            }
        )
    return out


def save_figures(
    outdir: Path,
    rows: list[dict[str, Any]],
    cycles: list[dict[str, Any]],
    trials: list[dict[str, Any]],
) -> list[str]:
    fig_names = []
    plt.rcParams.update(
        {"font.size": 10, "axes.titlesize": 12, "axes.labelsize": 10, "figure.dpi": 150}
    )

    # Figure 1: paired first request and average of five warm requests in each cycle.
    valid = [
        c
        for c in cycles
        if c.get("first_request_latency_ms") is not None
        and c.get("warm_average_latency_ms_success_only") is not None
    ]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    x = list(range(1, len(valid) + 1))
    for i, c in enumerate(valid):
        ax.plot(
            [i + 1, i + 1],
            [c["warm_average_latency_ms_success_only"], c["first_request_latency_ms"]],
            marker="o",
            linewidth=1.5,
        )
    ax.scatter(
        x,
        [c["first_request_latency_ms"] for c in valid],
        label="First request",
        marker="o",
        zorder=3,
    )
    ax.scatter(
        x,
        [c["warm_average_latency_ms_success_only"] for c in valid],
        label="Mean of five warm requests",
        marker="s",
        zorder=3,
    )
    ax.set_title("First-Request and Warm-Request Latency by Cycle")
    ax.set_xlabel("Cycle (grouped by trial in sequence)")
    ax.set_ylabel("HTTP request duration (ms)")
    ax.set_xticks(
        x,
        [f"{c['trial_id'].replace('trial-', 'T')}-C{c['cycle_id']}" for c in valid],
        rotation=45,
        ha="right",
    )
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    name = "figure_01_paired_cycle_comparison.png"
    fig.savefig(outdir / name)
    plt.close(fig)
    fig_names.append(name)

    # Figure 2: distributions of successful individual request latencies.
    first = [
        r["http_req_duration_ms"]
        for r in rows
        if r["request_class"] == "first"
        and r["http_success"]
        and r.get("http_req_duration_ms") is not None
    ]
    warm = [
        r["http_req_duration_ms"]
        for r in rows
        if r["request_class"] == "warm"
        and r["http_success"]
        and r.get("http_req_duration_ms") is not None
    ]
    fig, ax = plt.subplots(figsize=(7.5, 5.2))
    ax.boxplot(
        [first, warm],
        tick_labels=[
            f"First requests\n(n={len(first)})",
            f"Warm requests\n(n={len(warm)})",
        ],
        showmeans=True,
    )
    ax.set_title("Distribution of Successful Request Durations")
    ax.set_ylabel("HTTP request duration (ms)")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    name = "figure_02_latency_distribution.png"
    fig.savefig(outdir / name)
    plt.close(fig)
    fig_names.append(name)

    # Figure 3: trial means, retaining trial-level reproducibility in the presentation.
    labels = [t["scope"] for t in trials]
    first_means = [t.get("first_latency_mean_ms") for t in trials]
    warm_means = [t.get("warm_latency_mean_ms") for t in trials]
    positions = list(range(len(labels)))
    width = 0.36
    fig, ax = plt.subplots(figsize=(8.5, 5.2))
    ax.bar(
        [p - width / 2 for p in positions], first_means, width, label="First requests"
    )
    ax.bar([p + width / 2 for p in positions], warm_means, width, label="Warm requests")
    ax.set_xticks(positions, labels)
    ax.set_title("Mean Latency by Trial")
    ax.set_ylabel("Mean HTTP request duration (ms)")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    name = "figure_03_trial_mean_comparison.png"
    fig.savefig(outdir / name)
    plt.close(fig)
    fig_names.append(name)
    return fig_names


def fmt(v: Any, digits: int = 2) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


def write_report(
    path: Path,
    overall: dict[str, Any],
    trials: list[dict[str, Any]],
    issues: list[str],
    figures: list[str],
    expected_cycles: int,
    expected_warm: int,
    idle_seconds: int,
    wait_tolerance_ms: int,
) -> None:
    lines = [
        "# SBIMS Cold-Start Experiment: Consolidated Analysis",
        "",
        "## Dataset and validation",
        "",
        f"The analysis consolidated {len(trials)} formal trial files. The expected design was {expected_cycles} cycles per trial, with one first request and {expected_warm} sequential warm requests per cycle. The configured inactivity target was {idle_seconds / 60:.0f} minutes, with a {wait_tolerance_ms / 1000:.0f}-second timing tolerance.",
        f"The combined dataset contains {overall['first_requests_total']} first-request observations and {overall['warm_requests_total']} warm-request observations ({overall['request_count']} SBIMS endpoint requests). Supabase Auth token-preparation requests were excluded from endpoint latency analysis.",
        f"Successful SBIMS requests: {overall['successful_requests']}/{overall['request_count']}; failed requests: {overall['failed_requests']}.",
        "",
        "## Combined descriptive results",
        "",
        "| Measure | First requests | Warm requests |",
        "|---|---:|---:|",
        f"| Observations | {overall['first_requests_total']} | {overall['warm_requests_total']} |",
        f"| Successful | {overall['first_requests_successful']} | {overall['warm_requests_successful']} |",
        f"| Mean duration (ms) | {fmt(overall.get('first_latency_mean_ms'))} | {fmt(overall.get('warm_latency_mean_ms'))} |",
        f"| Median duration (ms) | {fmt(overall.get('first_latency_median_ms'))} | {fmt(overall.get('warm_latency_median_ms'))} |",
        f"| Standard deviation (ms) | {fmt(overall.get('first_latency_sd_ms'))} | {fmt(overall.get('warm_latency_sd_ms'))} |",
        f"| Minimum (ms) | {fmt(overall.get('first_latency_min_ms'))} | {fmt(overall.get('warm_latency_min_ms'))} |",
        f"| Maximum (ms) | {fmt(overall.get('first_latency_max_ms'))} | {fmt(overall.get('warm_latency_max_ms'))} |",
        f"| Mean TTFB (ms) | {fmt(overall.get('first_ttfb_mean_ms'))} | {fmt(overall.get('warm_ttfb_mean_ms'))} |",
        "",
    ]
    fm, wm = overall.get("first_latency_mean_ms"), overall.get("warm_latency_mean_ms")
    if fm is not None and wm:
        lines.append(
            f"The first-request mean was {fm / wm:.3f} times the warm-request mean, equivalent to a {(fm - wm) / wm * 100:.2f}% increase based on the ratio of the two pooled means."
        )
    lines += [
        "",
        "## Results by trial",
        "",
        "| Trial | First mean (ms) | Warm mean (ms) | Valid paired cycles | First slower | Mean paired increase (%) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for t in trials:
        lines.append(
            f"| {t['scope']} | {fmt(t.get('first_latency_mean_ms'))} | {fmt(t.get('warm_latency_mean_ms'))} | {t.get('valid_paired_cycles', 0)} | {t.get('first_slower_paired_cycles', 0)} | {fmt(t.get('mean_cycle_relative_increase_pct'))} |"
        )
    lines += [
        "",
        "## Interpretation notes",
        "",
        "- Request-duration summaries use successful HTTP 2xx endpoint responses only. Failed requests remain in the request-level and failure-analysis files and are not silently removed from the dataset.",
        "- The paired cycle comparison contrasts one first request with the mean of the five warm requests from that same cycle.",
        f"- The recorded controlled-wait range was {overall.get('controlled_wait_min_minutes'):.4f} to {overall.get('controlled_wait_max_minutes'):.4f} minutes. Values were checked against the configured {idle_seconds / 60:.0f}-minute target using the stated {wait_tolerance_ms / 1000:.0f}-second timing tolerance.",
        "- The mean percentage increase based on pooled means is distinct from the mean of cycle-level percentage increases; the report labels these separately.",
        "- `http_req_duration` is the k6 HTTP request duration (sending, waiting, and receiving); connection establishment and TLS handshake are separate metrics.",
        "- TTFB (`http_req_waiting`) includes time waiting for the response and does not isolate application startup.",
        "- Application initialization-age values are supporting application-level measurements, not direct proof of Deno Deploy instance termination or restart.",
        "- Findings describe the tested endpoint and experimental conditions; they are not a platform startup-time measurement or a whole-system performance assessment.",
        "",
        "## Input quality checks",
        "",
    ]
    if issues:
        lines += ["The following checks require review:", ""] + [
            f"- {x}" for x in issues
        ]
    else:
        lines.append(
            "All three trial files met the expected cycle and request-count structure, and controlled-wait measurements were present for every cycle."
        )
    lines += ["", "## Generated figures", ""] + [f"- `{x}`" for x in figures]
    lines += [
        "",
        "## Supporting output files",
        "",
        "- `request_level.csv`: all SBIMS endpoint request observations and timing/initialization fields.",
        "- `cycle_summary.csv`: paired first-versus-warm comparison for each cycle.",
        "- `trial_summary.csv`: separate descriptive results for each trial.",
        "- `overall_summary.csv`: consolidated descriptive results across all trials.",
        "- `initialization_summary.csv`: initialization-age and header-presence observations.",
        "- `failure_analysis.csv`: every failed or non-2xx endpoint request (header-only when none).",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consolidate and visualize the three formal SBIMS cold-start trials."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project root containing results/input (default: parent of analysis/).",
    )
    parser.add_argument(
        "--input",
        nargs="*",
        type=Path,
        help="Explicit raw k6 NDJSON input files; default is trial-01 through trial-03.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: PROJECT_ROOT/results/analysis).",
    )
    parser.add_argument(
        "--cycles", type=int, default=5, help="Expected cycles per trial (default: 5)."
    )
    parser.add_argument(
        "--warm-requests",
        type=int,
        default=5,
        help="Expected warm requests per cycle (default: 5).",
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=900,
        help="Configured inactivity target in seconds (default: 900).",
    )
    parser.add_argument(
        "--wait-tolerance-ms",
        type=int,
        default=5000,
        help="Permitted timer tolerance below the configured wait (default: 5000 ms).",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Write outputs despite missing/incorrect expected observations; issues are recorded in report.",
    )
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    input_paths = (
        [p if p.is_absolute() else project_root / p for p in args.input]
        if args.input
        else [
            project_root / "results/input" / f"trial-{i:02d}.json" for i in range(1, 4)
        ]
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else project_root / "results/analysis"
    )
    missing = [str(p) for p in input_paths if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing input file(s):\n- " + "\n- ".join(missing))

    all_rows: list[dict[str, Any]] = []
    cycle_values: dict[tuple[str, int], dict[str, float]] = {}
    for path in input_paths:
        rows, cm = load_trial(path)
        if not rows:
            raise ValueError(f"No SBIMS endpoint request records found in {path}")
        all_rows.extend(rows)
        cycle_values.update(cm)

    issues = validate_trials(
        all_rows,
        cycle_values,
        args.cycles,
        args.warm_requests,
        args.idle_seconds,
        args.wait_tolerance_ms,
        strict=not args.allow_incomplete,
    )
    cycle_rows = make_cycle_rows(all_rows, cycle_values)
    trial_ids = sorted({r["trial_id"] for r in all_rows})
    trial_rows = [
        summarize_scope(t, [r for r in all_rows if r["trial_id"] == t], cycle_rows)
        for t in trial_ids
    ]
    overall = summarize_scope("all_trials", all_rows, cycle_rows)
    init_rows = initialization_rows(all_rows)
    failures = [{k: v for k, v in r.items()} for r in all_rows if not r["http_success"]]

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "request_level.csv", all_rows)
    write_csv(output_dir / "cycle_summary.csv", cycle_rows)
    write_csv(output_dir / "trial_summary.csv", trial_rows)
    write_csv(output_dir / "overall_summary.csv", [overall])
    write_csv(output_dir / "initialization_summary.csv", init_rows)
    failure_columns = (
        list(all_rows[0].keys())
        if all_rows
        else ["trial_id", "cycle_id", "request_class"]
    )
    if failures:
        write_csv(output_dir / "failure_analysis.csv", failures)
    else:
        with (output_dir / "failure_analysis.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as f:
            csv.writer(f).writerow(failure_columns)
    figures = save_figures(output_dir, all_rows, cycle_rows, trial_rows)
    write_report(
        output_dir / "analysis_report.md",
        overall,
        trial_rows,
        issues,
        figures,
        args.cycles,
        args.warm_requests,
        args.idle_seconds,
        args.wait_tolerance_ms,
    )

    print(f"Trials analyzed: {len(trial_ids)} ({', '.join(trial_ids)})")
    print(
        f"SBIMS endpoint requests: {overall['request_count']} ({overall['first_requests_total']} first; {overall['warm_requests_total']} warm)"
    )
    print(
        f"Successful requests: {overall['successful_requests']}; failed requests: {overall['failed_requests']}"
    )
    print(
        f"First mean: {fmt(overall.get('first_latency_mean_ms'))} ms; warm mean: {fmt(overall.get('warm_latency_mean_ms'))} ms"
    )
    print(f"Outputs: {output_dir}")
    if issues:
        print("Validation issues:", *issues, sep="\n- ", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
