# SBIMS Cold-Start Experiment: Consolidated Analysis

## Dataset and validation

The analysis consolidated 3 formal trial files. The expected design was 5 cycles per trial, with one first request and 5 sequential warm requests per cycle. The configured inactivity target was 15 minutes, with a 5-second timing tolerance.
The combined dataset contains 15 first-request observations and 75 warm-request observations (90 SBIMS endpoint requests). Supabase Auth token-preparation requests were excluded from endpoint latency analysis.
Successful SBIMS requests: 90/90; failed requests: 0.

## Combined descriptive results

| Measure                 | First requests | Warm requests |
| ----------------------- | -------------: | ------------: |
| Observations            |             15 |            75 |
| Successful              |             15 |            75 |
| Mean duration (ms)      |       4,044.76 |      1,749.10 |
| Median duration (ms)    |       3,891.05 |      1,665.38 |
| Standard deviation (ms) |         678.00 |        238.91 |
| Minimum (ms)            |       3,273.31 |      1,485.23 |
| Maximum (ms)            |       6,044.10 |      2,662.34 |
| Mean TTFB (ms)          |       4,044.45 |      1,748.87 |

The first-request mean was 2.312 times the warm-request mean, equivalent to a 131.25% increase based on the ratio of the two pooled means.

## Results by trial

| Trial    | First mean (ms) | Warm mean (ms) | Valid paired cycles | First slower | Mean paired increase (%) |
| -------- | --------------: | -------------: | ------------------: | -----------: | -----------------------: |
| trial-01 |        4,333.93 |       1,809.75 |                   5 |            5 |                   138.57 |
| trial-02 |        4,018.98 |       1,753.98 |                   5 |            5 |                   129.14 |
| trial-03 |        3,781.35 |       1,683.56 |                   5 |            5 |                   124.63 |

## Interpretation notes

- Request-duration summaries use successful HTTP 2xx endpoint responses only. Failed requests remain in the request-level and failure-analysis files and are not silently removed from the dataset.
- The paired cycle comparison contrasts one first request with the mean of the five warm requests from that same cycle.
- The recorded controlled-wait range was 14.9996 to 56.5854 minutes. Values were checked against the configured 15-minute target using the stated 5-second timing tolerance.
- The mean percentage increase based on pooled means is distinct from the mean of cycle-level percentage increases; the report labels these separately.
- `http_req_duration` is the k6 HTTP request duration (sending, waiting, and receiving); connection establishment and TLS handshake are separate metrics.
- TTFB (`http_req_waiting`) includes time waiting for the response and does not isolate application startup.
- Application initialization-age values are supporting application-level measurements, not direct proof of Deno Deploy instance termination or restart.
- Findings describe the tested endpoint and experimental conditions; they are not a platform startup-time measurement or a whole-system performance assessment.

## Input quality checks

All three trial files met the expected cycle and request-count structure, and controlled-wait measurements were present for every cycle.

## Generated figures

- `figure_01_paired_cycle_comparison.png`
- `figure_02_latency_distribution.png`
- `figure_03_trial_mean_comparison.png`

## Supporting output files

- `request_level.csv`: all SBIMS endpoint request observations and timing/initialization fields.
- `cycle_summary.csv`: paired first-versus-warm comparison for each cycle.
- `trial_summary.csv`: separate descriptive results for each trial.
- `overall_summary.csv`: consolidated descriptive results across all trials.
- `initialization_summary.csv`: initialization-age and header-presence observations.
- `failure_analysis.csv`: every failed or non-2xx endpoint request (header-only when none).
