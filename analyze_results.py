from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

METRIC_COLUMNS = {
    "http_req_duration": "http_req_duration_ms",
    "http_req_waiting": "http_req_waiting_ms",
    "http_req_connecting": "http_req_connecting_ms",
    "http_req_tls_handshaking": "http_req_tls_handshaking_ms",
    "http_req_failed": "http_req_failed",
}


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def load_k6_points(path: Path) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, float]]]:
    """Return SBIMS request rows and cycle-level custom metrics."""
    requests: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    cycle_metrics: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                point = json.loads(line)
            except json.JSONDecodeError:
                print(f"Warning: skipping invalid JSON at {path}:{line_number}")
                continue

            metric = point.get("metric")
            data = point.get("data") or {}
            tags = data.get("tags") or {}
            value = safe_float(data.get("value"))
            timestamp = str(data.get("time", ""))

            if metric in ("sbims_idle_gap_ms", "sbims_controlled_wait_ms"):
                trial = str(tags.get("trial_id", ""))
                cycle = str(tags.get("cycle_id", ""))
                if trial and cycle and value is not None:
                    cycle_metrics[(trial, cycle)][metric] = value
                continue

            # Ignore Supabase token-preparation requests and all unrelated metrics.
            if metric not in METRIC_COLUMNS or tags.get("name") != "sbims_students_me":
                continue

            request_class = str(tags.get("request_class", ""))
            if request_class not in {"first", "warm"}:
                continue

            trial = str(tags.get("trial_id", ""))
            cycle = str(tags.get("cycle_id", ""))
            sequence = str(tags.get("request_sequence", ""))
            if not trial or not cycle or not timestamp:
                continue

            key = (trial, cycle, request_class, timestamp)
            row = requests.setdefault(
                key,
                {
                    "trial_id": trial,
                    "cycle_id": int(cycle),
                    "request_class": request_class,
                    "request_sequence": int(sequence) if sequence.isdigit() else None,
                    "timestamp": timestamp,
                    "http_status": tags.get("status", ""),
                },
            )
            if value is not None:
                row[METRIC_COLUMNS[metric]] = value

    request_rows = list(requests.values())
    request_rows.sort(key=lambda row: (row["trial_id"], row["cycle_id"], row["timestamp"]))
    return request_rows, cycle_metrics


def mean_or_none(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def stdev_or_none(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def summarize_group(rows: list[dict[str, Any]], group_name: str) -> dict[str, Any]:
    first_values = [
        float(r["http_req_duration_ms"])
        for r in rows
        if r["request_class"] == "first" and r.get("http_req_duration_ms") is not None
    ]
    warm_values = [
        float(r["http_req_duration_ms"])
        for r in rows
        if r["request_class"] == "warm" and r.get("http_req_duration_ms") is not None
    ]
    differences: list[float] = []
    relative_increases: list[float] = []

    by_cycle: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cycle[(row["trial_id"], row["cycle_id"])].append(row)

    for cycle_rows in by_cycle.values():
        first = [r for r in cycle_rows if r["request_class"] == "first"]
        warm = [
            float(r["http_req_duration_ms"])
            for r in cycle_rows
            if r["request_class"] == "warm" and r.get("http_req_duration_ms") is not None
        ]
        if first and first[0].get("http_req_duration_ms") is not None and warm:
            first_latency = float(first[0]["http_req_duration_ms"])
            warm_average = statistics.mean(warm)
            differences.append(first_latency - warm_average)
            if warm_average > 0:
                relative_increases.append((first_latency - warm_average) / warm_average * 100)

    statuses = [str(r.get("http_status", "")) for r in rows]
    status_known = [s for s in statuses if s]
    successes = sum(1 for s in status_known if s.isdigit() and 200 <= int(s) < 300)
    failures = len(status_known) - successes

    return {
        group_name: None,
        "first_request_count": len(first_values),
        "warm_request_count": len(warm_values),
        "first_latency_mean_ms": mean_or_none(first_values),
        "first_latency_median_ms": median_or_none(first_values),
        "first_latency_sd_ms": stdev_or_none(first_values),
        "first_latency_min_ms": min(first_values) if first_values else None,
        "first_latency_max_ms": max(first_values) if first_values else None,
        "warm_latency_mean_ms": mean_or_none(warm_values),
        "warm_latency_median_ms": median_or_none(warm_values),
        "warm_latency_sd_ms": stdev_or_none(warm_values),
        "mean_cycle_latency_difference_ms": mean_or_none(differences),
        "mean_cycle_relative_increase_pct": mean_or_none(relative_increases),
        "http_2xx_count": successes,
        "http_non_2xx_count": failures,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze SBIMS k6 cold-start profiling results.")
    parser.add_argument(
        "--input",
        nargs="+",
        default=["results/trial-01.json", "results/trial-02.json"],
        help="One or more raw k6 JSON output files.",
    )
    parser.add_argument("--output-dir", default="results", help="Directory for CSV outputs.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_requests: list[dict[str, Any]] = []
    cycle_metric_map: dict[tuple[str, str], dict[str, float]] = {}

    for input_name in args.input:
        input_path = Path(input_name)
        if not input_path.exists():
            raise SystemExit(f"Input file not found: {input_path}")
        request_rows, metrics = load_k6_points(input_path)
        if not request_rows:
            print(f"Warning: no SBIMS request measurements found in {input_path}.")
        all_requests.extend(request_rows)
        cycle_metric_map.update(metrics)

    all_requests.sort(key=lambda r: (r["trial_id"], r["cycle_id"], r["timestamp"]))

    # Attach controlled wait and observed inter-cycle idle gap to first-request rows.
    for row in all_requests:
        if row["request_class"] == "first":
            metrics = cycle_metric_map.get((row["trial_id"], str(row["cycle_id"])), {})
            row["controlled_wait_ms"] = metrics.get("sbims_controlled_wait_ms")
            row["elapsed_since_previous_sbims_request_ms"] = metrics.get("sbims_idle_gap_ms")

    write_csv(output_dir / "request_level.csv", all_requests)

    # Build one row per trial-cycle, including paired first/warm measurements.
    cycle_rows: list[dict[str, Any]] = []
    cycle_groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in all_requests:
        cycle_groups[(row["trial_id"], row["cycle_id"])].append(row)

    for (trial, cycle), rows in sorted(cycle_groups.items()):
        first_rows = [r for r in rows if r["request_class"] == "first"]
        warm_rows = [
            r for r in rows
            if r["request_class"] == "warm" and r.get("http_req_duration_ms") is not None
        ]
        first_latency = (
            float(first_rows[0]["http_req_duration_ms"])
            if first_rows and first_rows[0].get("http_req_duration_ms") is not None
            else None
        )
        warm_latencies = [float(r["http_req_duration_ms"]) for r in warm_rows]
        warm_average = mean_or_none(warm_latencies)
        difference = first_latency - warm_average if first_latency is not None and warm_average is not None else None
        relative = (difference / warm_average * 100) if difference is not None and warm_average and warm_average > 0 else None
        cycle_metrics = cycle_metric_map.get((trial, str(cycle)), {})
        cycle_rows.append({
            "trial_id": trial,
            "cycle_id": cycle,
            "first_request_latency_ms": first_latency,
            "warm_request_count": len(warm_latencies),
            "warm_average_latency_ms": warm_average,
            "warm_median_latency_ms": median_or_none(warm_latencies),
            "latency_difference_ms": difference,
            "relative_increase_pct": relative,
            "controlled_wait_ms": cycle_metrics.get("sbims_controlled_wait_ms"),
            "elapsed_since_previous_sbims_request_ms": cycle_metrics.get("sbims_idle_gap_ms"),
            "first_request_status": first_rows[0].get("http_status") if first_rows else None,
            "first_request_failed_metric": first_rows[0].get("http_req_failed") if first_rows else None,
        })

    write_csv(output_dir / "cycle_summary.csv", cycle_rows)

    trial_ids = sorted({r["trial_id"] for r in all_requests})
    trial_summaries: list[dict[str, Any]] = []
    for trial in trial_ids:
        rows = [r for r in all_requests if r["trial_id"] == trial]
        summary = summarize_group(rows, "trial_id")
        summary["trial_id"] = trial
        trial_summaries.append(summary)
    write_csv(output_dir / "trial_summary.csv", trial_summaries)

    overall = summarize_group(all_requests, "scope")
    overall["scope"] = "all_trials"
    write_csv(output_dir / "overall_summary.csv", [overall])

    print(f"Analyzed {len(all_requests)} SBIMS request observations.")
    print(f"First-request observations: {sum(1 for r in all_requests if r['request_class'] == 'first')}")
    print(f"Warm-request observations: {sum(1 for r in all_requests if r['request_class'] == 'warm')}")
    print(f"CSV outputs saved to: {output_dir.resolve()}")
    print("Review missing cycles, failed requests, and status codes before interpreting the results.")


if __name__ == "__main__":
    main()
