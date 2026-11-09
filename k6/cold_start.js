import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate } from "k6/metrics";

/*
 * SBIMS first-request latency following controlled inactivity.
 *
 * One k6 execution = one trial.
 * One trial = five cycles by default.
 *
 * Each cycle:
 * 1. Wait for the configured inactivity period.
 * 2. Obtain a fresh Supabase access token directly from Supabase Auth.
 * 3. Send one first SBIMS request.
 * 4. Send five sequential warm SBIMS requests.
 *
 * Supabase token preparation does not call the SBIMS backend.
 *
 * Formal defaults:
 *   CYCLES_PER_TRIAL = 5
 *   WARM_REQUESTS = 5
 *   IDLE_SECONDS = 900
 *
 * Short waits are permitted only when VALIDATION_MODE=true.
 */

// --------------------------------------------------
// Configuration
// --------------------------------------------------

const API_BASE_URL = (
  __ENV.API_BASE_URL || "https://api.sbims.me/api/v1"
).replace(/\/+$/, "");

const STUDENTS_ME_URL = `${API_BASE_URL}/students/me`;

const SUPABASE_URL = (
  __ENV.SUPABASE_URL || ""
).replace(/\/+$/, "");

const SUPABASE_ANON_KEY =
  __ENV.SUPABASE_ANON_KEY || "";
const STUDENT_EMAIL = __ENV.STUDENT_EMAIL || "";
const STUDENT_PASSWORD = __ENV.STUDENT_PASSWORD || "";

const TRIAL_ID = __ENV.TRIAL_ID || "trial-03";

const VALIDATION_MODE = __ENV.VALIDATION_MODE === "true";

const CYCLES = Number(__ENV.CYCLES_PER_TRIAL || 5);
const WARM_REQUESTS = Number(__ENV.WARM_REQUESTS || 5);
const IDLE_SECONDS = Number(__ENV.IDLE_SECONDS || 900);

// --------------------------------------------------
// Custom metrics
// --------------------------------------------------

const idleGapMs = new Trend("sbims_idle_gap_ms", true);
const controlledWaitMs = new Trend("sbims_controlled_wait_ms", true);

const firstRequestDurationMs = new Trend(
  "sbims_first_request_duration_ms",
  true,
);

const warmRequestDurationMs = new Trend("sbims_warm_request_duration_ms", true);

const applicationInitializationAgeMs = new Trend(
  "sbims_application_initialization_age_ms",
  true,
);

const sbimsRequestSuccess = new Rate("sbims_request_success");

const initializationHeadersPresent = new Rate(
  "sbims_initialization_headers_present",
);

// --------------------------------------------------
// k6 options
// --------------------------------------------------

export const options = {
  scenarios: {
    cold_start_profile: {
      executor: "per-vu-iterations",
      vus: 1,
      iterations: 1,
      maxDuration: "90m",
    },
  },

  summaryTrendStats: ["min", "med", "avg", "max", "p(90)", "p(95)"],
};

// --------------------------------------------------
// Configuration validation
// --------------------------------------------------

function requireConfiguration() {
  const missing = [];

  if (!SUPABASE_URL) missing.push("SUPABASE_URL");
  if (!SUPABASE_ANON_KEY) missing.push("SUPABASE_ANON_KEY");
  if (!STUDENT_EMAIL) missing.push("STUDENT_EMAIL");
  if (!STUDENT_PASSWORD) missing.push("STUDENT_PASSWORD");

  if (missing.length > 0) {
    throw new Error(
      `Missing required environment variables: ${missing.join(", ")}`,
    );
  }

  if (!Number.isInteger(CYCLES) || CYCLES < 1) {
    throw new Error("CYCLES_PER_TRIAL must be a positive integer.");
  }

  if (!Number.isInteger(WARM_REQUESTS) || WARM_REQUESTS < 1) {
    throw new Error("WARM_REQUESTS must be a positive integer.");
  }

  if (!Number.isFinite(IDLE_SECONDS) || IDLE_SECONDS < 1) {
    throw new Error("IDLE_SECONDS must be a positive number.");
  }

  if (IDLE_SECONDS < 900 && !VALIDATION_MODE) {
    throw new Error(
      "IDLE_SECONDS must be at least 900 seconds for formal testing. " +
        "Set VALIDATION_MODE=true only for short validation runs.",
    );
  }

  if (VALIDATION_MODE) {
    console.log(
      "VALIDATION MODE: Short inactivity periods are permitted. " +
        "Do not treat these results as formal experiment results.",
    );
  }
}

// --------------------------------------------------
// Supabase authentication
// --------------------------------------------------

function obtainSupabaseAccessToken(cycleId) {
  // This request goes directly to Supabase Auth.
  // It does not contact the SBIMS backend.

  const authUrl = `${SUPABASE_URL}/auth/v1/token?grant_type=password`;

  const response = http.post(
    authUrl,
    JSON.stringify({
      email: STUDENT_EMAIL,
      password: STUDENT_PASSWORD,
    }),
    {
      headers: {
        apikey: SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
        Accept: "application/json",
      },

      tags: {
        name: "supabase_auth_token_preparation",
        request_class: "authentication_preparation",
        trial_id: TRIAL_ID,
        cycle_id: String(cycleId),
      },

      redirects: 0,
    },
  );

  if (response.status !== 200) {
    console.error(`Supabase HTTP status: ${response.status}`);

    // Avoid printing authentication response bodies, which may
    // contain sensitive information.
    throw new Error(`Could not obtain access token for cycle ${cycleId}.`);
  }

  let payload;

  try {
    payload = response.json();
  } catch (_) {
    throw new Error(
      `Supabase returned a non-JSON response in cycle ${cycleId}.`,
    );
  }

  if (!payload || !payload.access_token) {
    throw new Error(
      `Supabase response did not contain access_token in cycle ${cycleId}.`,
    );
  }

  return payload.access_token;
}

// --------------------------------------------------
// Response header helper
// --------------------------------------------------

function getResponseHeader(response, headerName) {
  const target = headerName.toLowerCase();

  for (const key of Object.keys(response.headers || {})) {
    if (key.toLowerCase() === target) {
      return response.headers[key];
    }
  }

  return undefined;
}

// --------------------------------------------------
// SBIMS request
// --------------------------------------------------

function sendSbimsRequest(token, cycleId, requestClass, sequence) {
  const tags = {
    name: "sbims_students_me",
    trial_id: TRIAL_ID,
    cycle_id: String(cycleId),
    request_class: requestClass,
    request_sequence: String(sequence),
  };

  const response = http.get(STUDENTS_ME_URL, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
    },

    tags,
    redirects: 0,
  });

  const successful = response.status >= 200 && response.status < 300;

  sbimsRequestSuccess.add(successful, tags);

  check(
    response,
    {
      [`${requestClass} request returns HTTP 2xx`]: (r) =>
        r.status >= 200 && r.status < 300,
    },
    tags,
  );

  // Record response time separately for first and warm requests.
  if (requestClass === "first") {
    firstRequestDurationMs.add(response.timings.duration, tags);
  } else {
    warmRequestDurationMs.add(response.timings.duration, tags);
  }

  // Capture application initialization headers.
  const applicationId = getResponseHeader(response, "X-Application-ID");

  const initializationAgeHeader = getResponseHeader(
    response,
    "X-Application-Initialization-Age-Ms",
  );

  const headersPresent =
    applicationId !== undefined &&
    applicationId !== null &&
    String(applicationId).trim() !== "" &&
    initializationAgeHeader !== undefined &&
    initializationAgeHeader !== null &&
    String(initializationAgeHeader).trim() !== "";

  initializationHeadersPresent.add(headersPresent, tags);

  if (headersPresent) {
    const initializationAge = Number(initializationAgeHeader);

    if (Number.isFinite(initializationAge) && initializationAge >= 0) {
      applicationInitializationAgeMs.add(initializationAge, tags);
    } else {
      console.error(
        `Invalid initialization-age header in cycle ${cycleId}, ` +
          `${requestClass} request ${sequence}.`,
      );
    }

    // Log the ID and age for each response.
    // These values can be inspected in the raw k6 output.
    console.log(
      `SBIMS_HEADER trial=${TRIAL_ID} ` +
        `cycle=${cycleId} ` +
        `class=${requestClass} ` +
        `sequence=${sequence} ` +
        `application_id=${applicationId} ` +
        `initialization_age_ms=${initializationAgeHeader}`,
    );
  } else {
    console.error(
      `Missing application initialization headers: ` +
        `cycle=${cycleId}, class=${requestClass}, sequence=${sequence}`,
    );
  }

  return {
    response,
    completedAtMs: Date.now(),
  };
}

// --------------------------------------------------
// Main test execution
// --------------------------------------------------

export default function () {
  requireConfiguration();

  let previousSbimsRequestEndMs = null;

  const trialStartedAtMs = Date.now();

  for (let cycle = 1; cycle <= CYCLES; cycle += 1) {
    const firstTags = {
      trial_id: TRIAL_ID,
      cycle_id: String(cycle),
      request_class: "first",
    };

    console.log(`Starting cycle ${cycle}/${CYCLES} for ${TRIAL_ID}.`);

    // 1. Begin controlled inactivity period.
    const waitStartedAtMs = Date.now();

    sleep(IDLE_SECONDS);

    const waitElapsedMs = Date.now() - waitStartedAtMs;

    controlledWaitMs.add(waitElapsedMs, firstTags);

    // Allow a small timing tolerance to avoid unnecessary test termination.
    const waitToleranceMs = 5000;
    const minimumAcceptableWaitMs = IDLE_SECONDS * 1000 - waitToleranceMs;

    console.log(
      `Controlled inactivity completed: ` +
        `cycle=${cycle}, ` +
        `configured_wait_ms=${IDLE_SECONDS * 1000}, ` +
        `actual_wait_ms=${waitElapsedMs}`,
    );

    if (waitElapsedMs < minimumAcceptableWaitMs) {
      console.warn(
        `WARNING: Controlled inactivity was shorter than expected. ` +
          `Cycle ${cycle} will continue, but its waiting-time deviation ` +
          `must be considered during analysis. ` +
          `Actual wait: ${waitElapsedMs} ms.`,
      );
    }

    // 2. Obtain a fresh Supabase token AFTER the wait.
    const token = obtainSupabaseAccessToken(cycle);

    // 3. Measure the complete interval between SBIMS requests.
    // Measurement occurs after token preparation and immediately
    // before the next first SBIMS request.
    if (previousSbimsRequestEndMs !== null) {
      idleGapMs.add(Date.now() - previousSbimsRequestEndMs, firstTags);
    }

    // 4. Send the first SBIMS request.
    const first = sendSbimsRequest(token, cycle, "first", 1);

    previousSbimsRequestEndMs = first.completedAtMs;

    // 5. Send sequential warm requests.
    for (let warm = 1; warm <= WARM_REQUESTS; warm += 1) {
      const result = sendSbimsRequest(token, cycle, "warm", warm);

      previousSbimsRequestEndMs = result.completedAtMs;
    }

    console.log(`Completed cycle ${cycle}/${CYCLES}.`);
  }

  console.log(
    `Completed ${TRIAL_ID}: ` +
      `${CYCLES} cycles, ` +
      `${CYCLES} first requests, ` +
      `${CYCLES * WARM_REQUESTS} warm requests. ` +
      `Trial elapsed ${(Date.now() - trialStartedAtMs) / 1000}s.`,
  );
}

// --------------------------------------------------
// Summary output
// --------------------------------------------------

export function handleSummary(data) {
  const output = {};

  output[`results/k6-summary-${TRIAL_ID}.json`] = JSON.stringify(data, null, 2);

  return output;
}
