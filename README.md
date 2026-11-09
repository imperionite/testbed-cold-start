# SBIMS First-Request Latency Profiling (k6 + Python)

This package implements the proposed **first-request latency following controlled inactivity** experiment. It is a proxy for cold-start behavior; k6 response times alone cannot prove that Deno Deploy terminated and restarted a particular instance.

## Experimental design implemented

- One k6 virtual user.
- Two trials; run the script once for each trial.
- Five measurement cycles per trial.
- At least 12 minutes of inactivity before the first SBIMS request in each cycle.
- One first request followed by five sequential warm requests to the same endpoint: `/api/v1/students/me`.
- Authentication is prepared by calling Supabase Auth directly, **not** the SBIMS API. A fresh access token is obtained before each cycle so token expiry during a roughly one-hour trial is avoided.
- Raw k6 metric points are saved as JSON. Python consolidates request-level and cycle-level CSV files.

Expected total: 10 first-request observations and 50 subsequent-request observations across two trials.

## Important checks before running

1. Use a dedicated, active SBIMS student test account with an existing student profile.
2. Confirm the Supabase project URL and publishable/anon key from the project's deployment configuration. Do not use a Supabase service-role key.
3. Confirm that the account password is current and that the account is allowed to sign in directly through Supabase Auth.
4. Run only with permission to generate traffic against the deployed SBIMS API. Avoid other SBIMS activity and automated health checks during each controlled inactivity interval.
5. Use the same test machine and network for both trials. Record any interruptions, deployment changes, or unrelated traffic.
6. The script waits 12 minutes **inside each cycle**. A trial therefore takes a little over 60 minutes; two trials take a little over two hours.

## Environment variables

The script requires:

- `SUPABASE_URL` — Supabase project URL, e.g. `https://<project-ref>.supabase.co`.
- `SUPABASE_ANON_KEY` — Supabase publishable/anon key (not service-role key).
- `STUDENT_EMAIL` — dedicated student test-account email.
- `STUDENT_PASSWORD` — that account's current password.

Optional variables:

- `API_BASE_URL` — defaults to `https://api.sbims.me/api/v1`.
- `TRIAL_ID` — defaults to `trial-01`.
- `CYCLES_PER_TRIAL` — defaults to `5`.
- `WARM_REQUESTS` — defaults to `5`.
- `IDLE_SECONDS` — defaults to `720`; values below 720 are rejected.

Do not commit credentials to source control or paste them into the paper. Supply them through your local shell/session. k6 scripts read values passed with `--env`.

## Run on Windows PowerShell

Set the values in your current PowerShell session (replace placeholders locally):

```powershell
$env:SUPABASE_URL = "https://YOUR-PROJECT-REF.supabase.co"
$env:SUPABASE_ANON_KEY = "YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY"
$env:STUDENT_EMAIL = "YOUR_TEST_STUDENT_EMAIL"
$env:STUDENT_PASSWORD = "YOUR_TEST_STUDENT_PASSWORD"

New-Item -ItemType Directory -Force results | Out-Null

k6 run `
  --env SUPABASE_URL=$env:SUPABASE_URL `
  --env SUPABASE_ANON_KEY=$env:SUPABASE_ANON_KEY `
  --env STUDENT_EMAIL=$env:STUDENT_EMAIL `
  --env STUDENT_PASSWORD=$env:STUDENT_PASSWORD `
  --env TRIAL_ID=trial-01 `
  --out json=results/trial-01.json `
  k6/cold_start.js
```

After Trial 1 completes, run Trial 2:

```powershell
k6 run `
  --env SUPABASE_URL=$env:SUPABASE_URL `
  --env SUPABASE_ANON_KEY=$env:SUPABASE_ANON_KEY `
  --env STUDENT_EMAIL=$env:STUDENT_EMAIL `
  --env STUDENT_PASSWORD=$env:STUDENT_PASSWORD `
  --env TRIAL_ID=trial-02 `
  --out json=results/trial-02.json `
  k6/cold_start.js
```

If you prefer a different secure way to inject environment variables, use that, but ensure k6 receives them as `--env NAME=value` values. Avoid saving passwords in scripts or repository files.

## Run on macOS/Linux

Export the four required environment variables in your shell, create `results/`, then run:

```bash
mkdir -p results

k6 run \
  --env SUPABASE_URL="$SUPABASE_URL" \
  --env SUPABASE_ANON_KEY="$SUPABASE_ANON_KEY" \
  --env STUDENT_EMAIL="$STUDENT_EMAIL" \
  --env STUDENT_PASSWORD="$STUDENT_PASSWORD" \
  --env TRIAL_ID=trial-01 \
  --out json=results/trial-01.json \
  k6/cold_start.js
```

Repeat with `TRIAL_ID=trial-02` and output file `results/trial-02.json` for the second trial.

## Analyze results

After both trials finish, from this directory run:

```bash
python analyze_results.py
```

Or specify input/output paths:

```bash
python analyze_results.py --input results/input/trial-01.json results/input/trial-02.json --output-dir results/output
```

The analyzer creates:

- `request_level.csv` — each first/warm SBIMS request, HTTP status, total duration, waiting time, connection time, and TLS handshake time when present.
- `cycle_summary.csv` — one row per cycle, pairing its first-request latency with the average of its five warm requests.
- `trial_summary.csv` — descriptive summaries for each trial.
- `overall_summary.csv` — combined descriptive summary.

The raw k6 JSON files and `k6-summary-trial-*.json` files are retained as source evidence.

## Interpretation cautions

- The first request is a **cold-start candidate**, not a confirmed cold start.
- `http_req_duration` is end-to-end HTTP request duration. It includes request sending, server waiting, and response receiving; it is not isolated Deno startup time.
- The SBIMS `/students/me` route performs authentication and role checks and reads student/internship-related data. Therefore, observed latency includes Supabase Auth validation, profile/role checks, database queries, network conditions, and possible application initialization.
- The first request in Cycle 1 has no in-script timestamp for the last request before the trial began. Ensure no SBIMS traffic for at least 12 minutes before starting Trial 1 and document this. For Cycles 2–5, the script records elapsed time since the previous SBIMS request.
- The script obtains a new token directly from Supabase Auth before each cycle. This does not call the SBIMS/Deno API, but it does create activity at the Supabase Auth service.
- The five warm requests in a cycle are sequential and may reuse the same client connection. Connection-related metrics are exported to help interpret this, but they do not isolate server startup.
- A 12-minute idle period is based on the documented Deno Deploy inactivity window, but does not guarantee that the exact instance handling the next request was terminated. Correlate with deployment boot/startup logs if available.
- Ten first-request observations are suitable for descriptive profiling, not strong inferential claims. Avoid treating the 50 warm requests as 50 independent cold-start samples.
- If any request fails, retain and report it. Do not silently discard failed or unusually slow observations.
