# SBIMS First-Request Latency Profiling

This package implements the proposed **proxy for cold-start behavior or first-request latency following controlled inactivity** experiment.

## Experimental design implemented

- Target: deployed SBIMS API endpoint `GET /api/v1/students/me`.
- Tool: k6; one virtual user; sequential requests.
- Trials: three independent k6 executions.
- Each trial: five cycles.
- Each cycle: minimum 15-minute inactivity period, one first request, then five sequential warm requests.
- A fresh access token is obtained directly from Supabase Auth after the wait and before the first SBIMS request. Token preparation does not call the SBIMS API.
- Total expected observations: 15 first requests and 75 warm requests (90 SBIMS endpoint requests).

The test measures client-observed endpoint response behavior. It does not directly measure Deno Deploy startup time or prove that a particular platform instance was terminated and restarted.

## Requirements and credentials

- k6 installed locally.
- Python 3.10 or later.
- Matplotlib installed (`python -m pip install matplotlib`).
- A dedicated SBIMS student test account with a valid profile.
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `STUDENT_EMAIL`, and `STUDENT_PASSWORD` supplied through the local shell environment. Do not commit credentials or use a service-role key.

Optional k6 variables include `API_BASE_URL`, `TRIAL_ID`, `CYCLES_PER_TRIAL`, `WARM_REQUESTS`, and `IDLE_SECONDS`. Formal defaults are five cycles, five warm requests, and 900 seconds of inactivity. The script rejects waits below 900 seconds unless validation mode is explicitly enabled.

## Run the formal trials

From the project root, set the four required environment variables in your shell. Then run each trial separately. Example for macOS/Linux:

```bash
mkdir -p results/input

TRIAL_ID=trial-01 k6 run \
  --out json=results/input/trial-01.json \
  k6/cold_start.js

TRIAL_ID=trial-02 k6 run \
  --out json=results/input/trial-02.json \
  k6/cold_start.js

TRIAL_ID=trial-03 k6 run \
  --out json=results/input/trial-03.json \
  k6/cold_start.js
```

The four required credentials must already be exported in the shell before running k6. On Windows PowerShell, set them as `$env:SUPABASE_URL`, `$env:SUPABASE_ANON_KEY`, `$env:STUDENT_EMAIL`, and `$env:STUDENT_PASSWORD`, then run the same three k6 commands using PowerShell line continuation if needed.

Retain each raw JSON output unchanged. Record deployment changes, test-machine interruptions, and other SBIMS activity that may have occurred during the experiment.

## Consolidate and analyze all three trials

Install Matplotlib if it is not already installed:

```bash
python -m pip install matplotlib
```

From the project root, run:

```bash
python analysis/analyze_cold_start.py
```

The script reads `results/input/trial-01.json` through `trial-03.json` and writes the following to `results/analysis/`:

- `request_level.csv` — endpoint request records, status, latency components, and initialization measurements.
- `cycle_summary.csv` — paired first-request versus warm-request comparison for each of the 15 cycles.
- `trial_summary.csv` — separate descriptive results for each formal trial.
- `overall_summary.csv` — combined results across all three trials.
- `initialization_summary.csv` — initialization-age and header-presence summary.
- `failure_analysis.csv` — failed/non-2xx endpoint requests; contains only a header if no failures occurred.
- `figure_01_paired_cycle_comparison.png` — first request and mean of five warm requests for each cycle.
- `figure_02_latency_distribution.png` — successful first-request and warm-request latency distributions.
- `figure_03_trial_mean_comparison.png` — mean latency by trial and request class.
- `analysis_report.md` — automatically generated summary and interpretation notes.

The script checks the expected five cycles, one first request, five warm requests per cycle, request sequence numbers, and presence of controlled-wait measurements. It stops if the expected structure is incomplete. Use `--allow-incomplete` only when intentionally inspecting incomplete data; any validation issues will be listed in the report.

## Analysis rules and interpretation

- Only records tagged for the SBIMS `students/me` endpoint are included. Supabase Auth token-preparation requests are excluded from endpoint latency calculations.
- Successful-response latency summaries include HTTP 2xx requests with `http_req_failed=0`. Failed requests remain in the request-level data and are reported separately.
- The primary response measure is k6 `http_req_duration` (request sending, response waiting, and response receiving). Initial connection and TLS handshake times are separate metrics and are not included in this duration.
- `http_req_waiting` is reported as a supporting time-to-first-byte measure; it does not isolate application startup.
- Cycle-level comparisons pair one first request with the mean of the five warm requests in that same cycle. The mean percentage increase across cycles is not the same calculation as the percentage increase based on the two pooled request means; report each with its correct label.
- The first request is treated as a cold-start candidate following controlled inactivity. Application initialization headers/age provide supporting application-level observations, not direct proof of platform instance termination or restart.
- Results are descriptive and apply to this endpoint and test setup. Do not interpret them as the platform's exact startup time, the capacity of the hosting platform, or the performance of the entire SBIMS system.

## Evidence retention

Keep the three raw files in `results/input/` unchanged. Treat generated CSV files and figures as derived analysis outputs. Retain the analysis script and its execution environment/version information with the project so the results can be regenerated.
