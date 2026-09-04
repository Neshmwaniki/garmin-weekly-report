"""
Garmin Weekly Report — pulls the last 7 days of health/activity data from
Garmin Connect, computes every number deterministically in Python (averages,
week-over-week deltas, trend-based next-week projections from history),
hands that finished structured summary to Gemini for the qualitative coaching
narrative only, publishes an interactive dark-themed dashboard to GitHub
Pages, and emails a short, visual highlights card that links to it.
Runs every Sunday evening via GitHub Actions.

Design principle: Python owns every number on the page. Gemini never invents
or recomputes a statistic — it only interprets numbers it's handed and writes
the coaching/motivation copy. This keeps the numbers always correct and the
AI's job scoped to what AI is actually good at.

Required environment variables (set as GitHub Actions Secrets):
    GARMIN_EMAIL         — your Garmin Connect login email
    GARMIN_PASSWORD      — your Garmin Connect password
    GMAIL_USER           — gmail address that sends the report
    GMAIL_APP_PASSWORD   — 16-char Google app password (NOT your real password)
    RECIPIENT_EMAIL      — where the report gets sent (can equal GMAIL_USER)
    GEMINI_API_KEY       — Google Gemini API key for the coaching narrative
"""

REPORT_URL = "https://neshmwaniki.github.io/garmin-weekly-report/"

import csv
import io
import json
import os
import smtplib
import sys
import time
import urllib.parse
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google import genai
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


# ---------- helpers ----------

def env(name: str, default=None) -> str:
    value = os.environ.get(name)
    if not value:
        if default is not None:
            return default
        sys.exit(f"Missing required env var: {name}")
    return value


def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if cur is None or not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def seconds_to_hm(seconds):
    if not seconds:
        return "—"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}h {m}m"


def r1(v):
    """Round to 1 decimal, passing through None."""
    return round(v, 1) if isinstance(v, (int, float)) else v


def daily_hrv(row: dict):
    """HRV for one day, preferring the nightly overnight reading but falling
    back to Garmin's own rolling weekly average when the nightly figure is
    missing (avgOvernightHrv isn't populated for every device/night, but
    hrvSummary.weeklyAvg reliably is) — so a gap in one source doesn't blank
    out the whole week."""
    v = row.get("avg_overnight_hrv")
    return v if v is not None else row.get("hrv_weekly_avg")


# ---------- Garmin pulls ----------

def login() -> Garmin:
    """Login to Garmin Connect, backing off on rate-limit errors."""
    last_exc = None
    for attempt in range(3):
        try:
            client = Garmin(env("GARMIN_EMAIL"), env("GARMIN_PASSWORD"))
            client.login()
            return client
        except GarminConnectTooManyRequestsError as e:
            last_exc = e
            wait = 30 * (attempt + 1)
            print(f"Garmin login rate-limited (attempt {attempt + 1}). Waiting {wait}s...")
            time.sleep(wait)
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
            sys.exit(f"Garmin login failed: {e}")
    sys.exit(f"Garmin login failed after retries: {last_exc}")


def pull_week(client: Garmin, end: date):
    days = [end - timedelta(days=i) for i in range(6, -1, -1)]
    rows = []

    for d in days:
        iso = d.isoformat()
        row = {"date": iso, "weekday": d.strftime("%a")}

        try:
            sleep = client.get_sleep_data(iso)
            dto = safe_get(sleep, "dailySleepDTO", default={})
            row["sleep_score"] = safe_get(dto, "sleepScores", "overall", "value")
            row["sleep_seconds"] = safe_get(dto, "sleepTimeSeconds")
            row["deep_seconds"] = safe_get(dto, "deepSleepSeconds")
            row["light_seconds"] = safe_get(dto, "lightSleepSeconds")
            row["rem_seconds"] = safe_get(dto, "remSleepSeconds")
            row["awake_seconds"] = safe_get(dto, "awakeSleepSeconds")
            row["avg_overnight_hrv"] = safe_get(dto, "avgOvernightHrv")
        except Exception as e:
            print(f"  sleep pull failed for {iso}: {e}")

        try:
            stats = client.get_stats(iso)
            row["steps"] = stats.get("totalSteps")
            row["resting_hr"] = stats.get("restingHeartRate")
            row["avg_stress"] = stats.get("averageStressLevel")
            row["max_stress"] = stats.get("maxStressLevel")
            row["body_battery_high"] = stats.get("bodyBatteryHighestValue")
            row["body_battery_low"] = stats.get("bodyBatteryLowestValue")
            row["intensity_minutes"] = (
                (stats.get("moderateIntensityMinutes") or 0)
                + 2 * (stats.get("vigorousIntensityMinutes") or 0)
            )
        except Exception as e:
            print(f"  stats pull failed for {iso}: {e}")

        try:
            hrv = client.get_hrv_data(iso)
            row["hrv_weekly_avg"] = safe_get(hrv, "hrvSummary", "weeklyAvg")
            row["hrv_status"] = safe_get(hrv, "hrvSummary", "status")
        except Exception as e:
            print(f"  hrv pull failed for {iso}: {e}")

        try:
            readiness = client.get_training_readiness(iso)
            if readiness and isinstance(readiness, list) and readiness:
                row["training_readiness"] = readiness[0].get("score")
        except Exception as e:
            print(f"  readiness pull failed for {iso}: {e}")

        rows.append(row)

    activities = []
    try:
        start_iso = days[0].isoformat()
        end_iso = end.isoformat()
        acts = client.get_activities_by_date(start_iso, end_iso)
        for a in acts:
            activities.append({
                "date": a.get("startTimeLocal", "")[:10],
                "type": safe_get(a, "activityType", "typeKey"),
                "name": a.get("activityName"),
                "distance_km": round((a.get("distance") or 0) / 1000, 2),
                "duration_min": round((a.get("duration") or 0) / 60, 1),
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "calories": a.get("calories"),
            })
    except Exception as e:
        print(f"  activities pull failed: {e}")

    return rows, activities


# ---------- CSV helpers (used for the dashboard's on-request download buttons) ----------

def daily_to_csv(rows) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def activities_to_csv(acts) -> str:
    if not acts:
        return "no activities recorded\n"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(acts[0].keys()))
    writer.writeheader()
    writer.writerows(acts)
    return buf.getvalue()


# ---------- deterministic analysis (this is "the analysis" — all code, no AI) ----------

def compute_averages(rows) -> dict:
    """Weekly averages. Keys match the schema already used by 15 weeks of
    history in reports/*.json, so trend calculations work immediately."""

    def avg(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def total(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return sum(vals) if vals else None

    hrv_vals = [daily_hrv(r) for r in rows if daily_hrv(r) is not None]
    overnight_hrv = round(sum(hrv_vals) / len(hrv_vals), 1) if hrv_vals else None

    return {
        "sleep_score": avg("sleep_score"),
        "sleep_duration": seconds_to_hm(avg("sleep_seconds")),
        "deep_sleep": seconds_to_hm(avg("deep_seconds")),
        "rem_sleep": seconds_to_hm(avg("rem_seconds")),
        "resting_hr": avg("resting_hr"),
        "overnight_hrv": overnight_hrv,
        "steps": avg("steps"),
        "avg_stress": avg("avg_stress"),
        "body_battery_high": avg("body_battery_high"),
        "body_battery_low": avg("body_battery_low"),
        "hrv_weekly_avg": avg("hrv_weekly_avg"),
        "hrv_status": rows[-1].get("hrv_status") or "N/A",
        "weekly_intensity_minutes": total("intensity_minutes"),
        "training_readiness": rows[-1].get("training_readiness") or "N/A",
        "total_steps": total("steps"),
    }


# ---------- memory: read/write weekly snapshots for week-over-week comparison ----------

REPORTS_DIR = "reports"


def load_history(before_date: date, max_weeks: int = 10) -> list:
    """Load prior weekly reports (chronological order), oldest first, so
    trend calculations can use them. Excludes the current week itself."""
    if not os.path.isdir(REPORTS_DIR):
        return []

    history = []
    for fname in sorted(os.listdir(REPORTS_DIR)):
        if not fname.startswith("report_") or not fname.endswith(".json"):
            continue
        path = os.path.join(REPORTS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            week_end = date.fromisoformat(data["week_end"])
        except Exception as e:
            print(f"  skipping unreadable history file {fname}: {e}")
            continue
        if week_end < before_date:
            history.append(data)

    history.sort(key=lambda d: d["week_end"])
    return history[-max_weeks:]


def save_report_json(report_data: dict, end: date) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"report_{end.isoformat()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)
    print(f"Saved weekly snapshot to {path}")


# ---------- trends & next-week projections (deterministic, code-only) ----------

# key -> (display label, higher value is better?, lower clamp, upper clamp)
TREND_METRICS = [
    ("sleep_score", "Sleep Score", True, 0, 100),
    ("overnight_hrv", "HRV", True, 0, None),
    ("resting_hr", "Resting HR", False, 30, 100),
    ("avg_stress", "Stress", False, 0, 100),
    ("steps", "Steps/day", True, 0, None),
    ("weekly_intensity_minutes", "Intensity Minutes", True, 0, None),
    ("body_battery_high", "Body Battery High", True, 0, 100),
    ("body_battery_low", "Body Battery Low", True, 0, 100),
]


def linreg_slope(y: list) -> float:
    n = len(y)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(y) / n
    num = sum((xs[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def compute_metric_trends(history: list, current_averages: dict) -> dict:
    """For each tracked metric: delta vs last week, and — once there's
    enough history — a simple trend-line projection for next week."""
    result = {}
    for key, label, higher_is_better, lo, hi in TREND_METRICS:
        series = [
            h["averages"].get(key) for h in history
            if isinstance(h.get("averages", {}).get(key), (int, float))
        ]
        cur = current_averages.get(key)

        entry = {
            "label": label,
            "higher_is_better": higher_is_better,
            "current": cur,
            "history_weeks": len(series),
            "delta_vs_last_week": r1(cur - series[-1]) if (cur is not None and series) else None,
        }

        full_series = series + ([cur] if isinstance(cur, (int, float)) else [])
        if len(full_series) >= 3 and cur is not None:
            slope = linreg_slope(full_series[-6:])
            proj = cur + slope
            if lo is not None:
                proj = max(lo, proj)
            if hi is not None:
                proj = min(hi, proj)
            entry["trend_slope"] = round(slope, 2)
            entry["predicted_next_week"] = r1(proj)
        else:
            entry["trend_slope"] = None
            entry["predicted_next_week"] = None

        result[key] = entry
    return result


# ---------- Gemini coaching narrative (this is "the coaching" — AI only, no numbers invented) ----------

ANALYSIS_PROMPT = """You are an elite, no-nonsense performance coach. Below is a
fully computed weekly physiology summary for an athlete who trains with a Garmin
watch — every number has already been calculated for you. Your only job is to
interpret these numbers and motivate action. Do NOT invent, recompute, or restate
a different number than what's given below — reference the exact figures provided.

DATA (JSON):
{context}

Return ONLY a raw JSON object (no markdown fences, no commentary) with this exact
schema:
{{
  "week_headline": "One punchy, specific sentence capturing the week's shape — reference an actual number.",
  "metrics": {{
    "sleep": "2 sentences on sleep_score/overnight_hrv/sleep_duration and their trend. End with one imperative action.",
    "cardio_recovery": "2 sentences on resting_hr and HRV trend/status. End with one imperative action.",
    "activity_load": "2 sentences on steps/intensity_minutes/activities this week. End with one imperative action.",
    "stress_recovery": "2 sentences on avg_stress and body battery high/low cycling. End with one imperative action."
  }},
  "next_week_outlook": "1-2 sentences interpreting the predicted_next_week numbers given below — do not invent new numbers. If a metric has no prediction yet, say the baseline is still building.",
  "top_actions": ["Imperative action 1, specific and numeric", "Imperative action 2", "Imperative action 3"],
  "coach_take": "1-2 sentence closing line. Direct, energetic, motivating. No hedging, no corporate language."
}}

Tone: energetic and direct, like a great personal trainer who genuinely cares.
Every sentence should push the athlete toward one clear action. Be honest about
weak spots but frame them as opportunities. Never write generic filler like "it
is important to note."
"""

DEFAULT_ANALYSIS = {
    "week_headline": "Your numbers are in — dig into the dashboard for the full picture.",
    "metrics": {
        "sleep": "Sleep data compiled successfully. Review your sleep score and HRV trend on the dashboard.",
        "cardio_recovery": "Resting HR and HRV pulled successfully. Check the Trends tab for your trajectory.",
        "activity_load": "Steps and intensity minutes compiled. Review your activity load on the dashboard.",
        "stress_recovery": "Stress and Body Battery data compiled. Review your recovery cycles on the dashboard.",
    },
    "next_week_outlook": "Keep logging weeks — predictions sharpen as more history builds up.",
    "top_actions": [
        "Review your Trends tab and pick one metric to focus on this week.",
        "Prioritize consistent sleep and wake times.",
        "Schedule at least one full recovery day.",
    ],
    "coach_take": "The data's solid — now go act on it.",
}

GEMINI_MODEL_FALLBACKS = ["gemini-3.7-flash", "gemini-2.5-flash"]


def get_gemini_analysis(context: dict) -> dict:
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    context_json = json.dumps(context, indent=2, default=str)

    last_error = None
    for model_name in GEMINI_MODEL_FALLBACKS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=ANALYSIS_PROMPT.format(context=context_json),
            )
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                text = text.rsplit("```", 1)[0].strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            parsed = json.loads(text)
            # Make sure every expected key exists even if the model dropped one.
            merged = dict(DEFAULT_ANALYSIS)
            merged.update(parsed)
            merged["metrics"] = {**DEFAULT_ANALYSIS["metrics"], **parsed.get("metrics", {})}
            return merged
        except Exception as e:
            print(f"  Gemini model '{model_name}' failed: {e}")
            last_error = e

    print(f"All Gemini models failed ({last_error}); using fallback narrative.")
    return DEFAULT_ANALYSIS


# ---------- chart images for the email (email clients can't run JS/canvas) ----------

def get_chart_url(config: dict) -> str:
    encoded = urllib.parse.quote(json.dumps(config))
    return f"https://quickchart.io/chart?w=600&h=220&bkg=050505&c={encoded}"


def build_email_chart_url(rows: list) -> str:
    weekdays = [r.get("weekday", "") for r in rows]
    sleep_scores = [r.get("sleep_score") or 0 for r in rows]
    hrv_values = [daily_hrv(r) or 0 for r in rows]
    config = {
        "type": "bar",
        "data": {
            "labels": weekdays,
            "datasets": [
                {
                    "type": "bar",
                    "label": "Sleep Score",
                    "data": sleep_scores,
                    "backgroundColor": "rgba(34,197,94,0.75)",
                    "yAxisID": "y",
                },
                {
                    "type": "line",
                    "label": "HRV",
                    "data": hrv_values,
                    "borderColor": "#3B82F6",
                    "backgroundColor": "transparent",
                    "borderWidth": 2,
                    "pointRadius": 3,
                    "yAxisID": "y1",
                },
            ],
        },
        "options": {
            "legend": {"labels": {"fontColor": "#888888", "fontSize": 9}},
            "scales": {
                "yAxes": [
                    {"id": "y", "position": "left", "gridLines": {"color": "#1C1C1C"}, "ticks": {"min": 0, "max": 100, "fontColor": "#666", "fontSize": 9}},
                    {"id": "y1", "position": "right", "gridLines": {"display": False}, "ticks": {"fontColor": "#666", "fontSize": 9}},
                ],
                "xAxes": [{"gridLines": {"display": False}, "ticks": {"fontColor": "#FFFFFF", "fontSize": 9}}],
            },
        },
    }
    return get_chart_url(config)


# ---------- dashboard (GitHub Pages) — dark, interactive, tabs + charts ----------

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__PAGE_TITLE__</title>
<script src="assets/chart.umd.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    background: #050505; color: #FFFFFF; margin: 0; padding: 0 0 60px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 760px; margin: 0 auto; padding: 0 20px; }
  .topbar { height: 4px; background: linear-gradient(90deg, #22C55E, #10B981, #3B82F6); }
  .header { padding: 32px 0 20px; border-bottom: 1px solid #1C1C1C; }
  .badge {
    display: inline-block; background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.3);
    padding: 5px 12px; border-radius: 20px; font-size: 10px; color: #22C55E; font-weight: bold;
    letter-spacing: 0.18em; text-transform: uppercase; font-family: monospace;
  }
  h1 { font-size: 26px; font-weight: 800; letter-spacing: -0.03em; margin: 14px 0 4px; text-transform: uppercase; }
  .subtle { color: #666666; font-size: 12px; font-family: monospace; text-transform: uppercase; letter-spacing: 0.05em; }
  .headline { padding: 22px 0; font-size: 16px; line-height: 1.6; color: #EEEEEE; border-bottom: 1px solid #1C1C1C; }
  .tabs { display: flex; gap: 4px; padding: 16px 0; border-bottom: 1px solid #1C1C1C; flex-wrap: wrap; }
  .tab-btn {
    background: #0D0D0D; border: 1px solid #1C1C1C; color: #888; padding: 9px 16px; border-radius: 8px;
    font-family: monospace; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; cursor: pointer;
  }
  .tab-btn.active { background: #22C55E; color: #000; border-color: #22C55E; font-weight: bold; }
  .tab-panel { display: none; padding: 24px 0; }
  .tab-panel.active { display: block; }
  .section-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #888888;
    font-weight: bold; margin-bottom: 16px; font-family: monospace;
  }
  .card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .metric-card {
    background: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px; cursor: pointer;
  }
  .metric-card:hover { border-color: #333; }
  .metric-label { font-size: 10px; font-weight: bold; letter-spacing: 0.12em; text-transform: uppercase; font-family: monospace; }
  .metric-value { font-size: 26px; font-weight: 800; color: #fff; padding: 6px 0 2px; }
  .metric-value small { font-size: 12px; color: #666; font-weight: normal; }
  .metric-delta { font-size: 11.5px; font-family: monospace; }
  .metric-detail {
    display: none; margin-top: 12px; padding-top: 12px; border-top: 1px solid #1C1C1C;
    font-size: 13px; line-height: 1.55; color: #CCCCCC;
  }
  .metric-card.open .metric-detail { display: block; }
  .metric-card .chevron { float: right; color: #555; font-family: monospace; font-size: 11px; }
  .coach-box {
    border-left: 3px solid #22C55E; padding: 16px 20px; background: #090909; border-radius: 0 8px 8px 0; margin: 20px 0;
  }
  .coach-box .kicker { font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #22C55E; font-weight: bold; margin-bottom: 6px; font-family: monospace; }
  .coach-box p { font-style: italic; color: #EEEEEE; margin: 0; line-height: 1.55; }
  .actions li { margin-bottom: 10px; line-height: 1.5; font-size: 14px; color: #CCCCCC; }
  .actions li::marker { color: #22C55E; }
  .chart-card { background: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px; margin-bottom: 20px; }
  .chart-card canvas { max-height: 240px; }
  table.workouts { width: 100%; border-collapse: collapse; }
  table.workouts th { text-align: left; padding: 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #666; border-bottom: 2px solid #1C1C1C; font-family: monospace; }
  table.workouts td { padding: 10px 8px; font-size: 13px; border-bottom: 1px solid #1C1C1C; color: #ddd; }
  .predict-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  .predict-card { background: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 14px; }
  .predict-arrow { font-family: monospace; font-size: 13px; }
  .download-row { display: flex; gap: 10px; padding: 24px 0; border-top: 1px solid #1C1C1C; flex-wrap: wrap; }
  .dl-btn {
    background: #0D0D0D; border: 1px solid #1C1C1C; color: #ccc; padding: 10px 16px; border-radius: 8px;
    font-family: monospace; font-size: 11px; letter-spacing: 0.05em; cursor: pointer;
  }
  .dl-btn:hover { border-color: #22C55E; color: #22C55E; }
  .footer { padding: 20px 0; color: #333; font-size: 9px; text-transform: uppercase; letter-spacing: 0.12em; font-family: monospace; line-height: 1.7; }
</style>
</head>
<body>
<div class="topbar"></div>
<div class="wrap">
  <div class="header">
    <span class="badge">Calibrated Performance</span>
    <h1>Weekly Physiology Summary</h1>
    <div class="subtle">Week ending __WEEK_END__ &bull; __HISTORY_WEEKS__ weeks of history &bull; Device: Garmin Connect</div>
  </div>

  <div class="headline">__WEEK_HEADLINE__</div>

  <div class="tabs">
    <button class="tab-btn active" data-tab="week">This Week</button>
    <button class="tab-btn" data-tab="trends">Trends</button>
    <button class="tab-btn" data-tab="predictions">Predictions</button>
    <button class="tab-btn" data-tab="workouts">Workouts</button>
  </div>

  <div class="tab-panel active" id="tab-week">
    <div class="section-label">Biometric Performance Indexes — click a card for the coaching read</div>
    <div class="card-grid">
__METRIC_CARDS__
    </div>
    <div class="coach-box">
      <div class="kicker">Performance Coach Takeaway</div>
      <p>"__COACH_TAKE__"</p>
    </div>
    <div class="section-label">Top Actions This Week</div>
    <ul class="actions">
__ACTIONS_HTML__
    </ul>
  </div>

  <div class="tab-panel" id="tab-trends">
    <div class="section-label">Multi-Week Trends</div>
__TREND_CHARTS_HTML__
  </div>

  <div class="tab-panel" id="tab-predictions">
    <div class="coach-box">
      <div class="kicker">Next Week Outlook</div>
      <p>"__NEXT_WEEK_OUTLOOK__"</p>
    </div>
    <div class="section-label">Projected vs. This Week</div>
    <div class="predict-grid">
__PREDICTION_CARDS__
    </div>
  </div>

  <div class="tab-panel" id="tab-workouts">
    <div class="section-label">Workout &amp; Strain Logs</div>
    <table class="workouts">
      <thead><tr><th>Name</th><th>Distance</th><th>Duration</th><th>Avg HR</th><th>Calories</th></tr></thead>
      <tbody>
__ACTIVITIES_ROWS__
      </tbody>
    </table>
  </div>

  <div class="download-row">
    <button class="dl-btn" onclick="downloadCSV('daily_metrics.csv', DAILY_CSV)">&#8681; Daily metrics CSV</button>
    <button class="dl-btn" onclick="downloadCSV('activities.csv', ACTIVITIES_CSV)">&#8681; Activities CSV</button>
  </div>

  <div class="footer">
    Device pipeline: Garmin Connect API &bull; Coaching narrative: Gemini &bull;
    Generated __GENERATED_AT__ &bull; Runs every Sunday via GitHub Actions
  </div>
</div>

<script>
  // ---- tabs ----
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    });
  });

  // ---- metric card accordion ----
  document.querySelectorAll('.metric-card').forEach(card => {
    card.addEventListener('click', () => card.classList.toggle('open'));
  });

  // ---- CSV download on request (no attachments sent by email anymore) ----
  const DAILY_CSV = __DAILY_CSV_JSON__;
  const ACTIVITIES_CSV = __ACTIVITIES_CSV_JSON__;
  function downloadCSV(filename, content) {
    const blob = new Blob([content], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // ---- trend charts ----
  Chart.defaults.color = '#666666';
  Chart.defaults.borderColor = '#1C1C1C';
  const trendLabels = __TREND_LABELS_JSON__;
  const trendSeries = __TREND_SERIES_JSON__;
  const chartSpecs = [
    { id: 'chart-sleep', key: 'sleep_score', label: 'Sleep Score', color: '#22C55E' },
    { id: 'chart-hrv', key: 'overnight_hrv', label: 'HRV (ms)', color: '#3B82F6' },
    { id: 'chart-rhr', key: 'resting_hr', label: 'Resting HR', color: '#EF4444' },
  ];
  chartSpecs.forEach(spec => {
    const el = document.getElementById(spec.id);
    if (!el || !trendSeries[spec.key]) return;
    new Chart(el, {
      type: 'line',
      data: {
        labels: trendLabels,
        datasets: [{
          label: spec.label, data: trendSeries[spec.key], borderColor: spec.color,
          backgroundColor: spec.color + '22', fill: true, tension: 0.25, pointRadius: 3,
        }],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: { y: { grid: { color: '#1C1C1C' } }, x: { grid: { display: false } } },
      },
    });
  });
</script>
</body>
</html>"""


def _metric_delta_html(entry) -> str:
    delta = entry["delta_vs_last_week"]
    if delta is None:
        return '<span class="metric-delta" style="color:#555;">— no prior week yet</span>'
    good = (delta > 0) == entry["higher_is_better"] if delta != 0 else None
    if delta == 0:
        color, arrow = "#888", "→"
    else:
        color = "#22C55E" if good else "#EF4444"
        arrow = "▲" if delta > 0 else "▼"
    return f'<span class="metric-delta" style="color:{color};">{arrow} {abs(delta)} vs last week</span>'


def _build_metric_cards_html(trends: dict, averages: dict, ai_metrics: dict) -> str:
    units = {
        "sleep_score": ("", " /100"), "overnight_hrv": ("", " ms"), "resting_hr": ("", " bpm"),
        "avg_stress": ("", " /100"), "steps": ("", "/day"), "weekly_intensity_minutes": ("", " min"),
        "body_battery_high": ("", ""), "body_battery_low": ("", ""),
    }
    detail_map = {
        "sleep_score": ai_metrics.get("sleep", ""), "overnight_hrv": ai_metrics.get("sleep", ""),
        "resting_hr": ai_metrics.get("cardio_recovery", ""), "avg_stress": ai_metrics.get("stress_recovery", ""),
        "steps": ai_metrics.get("activity_load", ""), "weekly_intensity_minutes": ai_metrics.get("activity_load", ""),
        "body_battery_high": ai_metrics.get("stress_recovery", ""), "body_battery_low": ai_metrics.get("stress_recovery", ""),
    }
    color_map = {
        "sleep_score": "#3B82F6", "overnight_hrv": "#22C55E", "resting_hr": "#EF4444",
        "avg_stress": "#F59E0B", "steps": "#F59E0B", "weekly_intensity_minutes": "#F59E0B",
        "body_battery_high": "#3B82F6", "body_battery_low": "#3B82F6",
    }
    cards = []
    for key, label, *_ in TREND_METRICS:
        entry = trends[key]
        val = entry["current"]
        val_display = val if val is not None else "—"
        prefix, suffix = units.get(key, ("", ""))
        cards.append(f"""      <div class="metric-card">
        <span class="chevron">▾</span>
        <div class="metric-label" style="color:{color_map.get(key, '#888')};">{label}</div>
        <div class="metric-value">{prefix}{val_display}<small>{suffix}</small></div>
        {_metric_delta_html(entry)}
        <div class="metric-detail">{detail_map.get(key, '')}</div>
      </div>""")
    return "\n".join(cards)


def _build_actions_html(actions: list) -> str:
    return "\n".join(f"      <li>{a}</li>" for a in actions) or "      <li>Review your dashboard for this week's focus areas.</li>"


def _build_activities_rows(activities: list) -> str:
    if not activities:
        return '      <tr><td colspan="5" style="text-align:center;color:#555;font-style:italic;">No activities recorded this week.</td></tr>'
    rows = []
    for a in activities:
        dist = f"{a['distance_km']} km" if a.get("distance_km") else "—"
        rows.append(
            f'      <tr><td>{a.get("name") or a.get("type") or "Activity"}</td>'
            f'<td>{dist}</td><td>{a.get("duration_min", "—")} min</td>'
            f'<td>{a.get("avg_hr") or "—"} bpm</td><td>{a.get("calories") or "—"}</td></tr>'
        )
    return "\n".join(rows)


def _build_trend_charts_html(trends: dict) -> str:
    specs = [("chart-sleep", "sleep_score", "Weekly Sleep Score"), ("chart-hrv", "overnight_hrv", "Overnight HRV"), ("chart-rhr", "resting_hr", "Resting Heart Rate")]
    blocks = []
    for chart_id, key, title in specs:
        if trends[key]["history_weeks"] < 2:
            blocks.append(f'''    <div class="chart-card">
      <div class="section-label">{title}</div>
      <p style="color:#555;font-size:13px;">Building your baseline — check back after a couple more weeks of data.</p>
    </div>''')
        else:
            blocks.append(f'''    <div class="chart-card">
      <div class="section-label">{title}</div>
      <canvas id="{chart_id}"></canvas>
    </div>''')
    return "\n".join(blocks)


def _build_prediction_cards(trends: dict) -> str:
    cards = []
    for key, label, higher_is_better, *_ in TREND_METRICS:
        entry = trends[key]
        cur = entry["current"]
        proj = entry["predicted_next_week"]
        if proj is None:
            body = f'<div style="color:#555;font-size:12px;margin-top:6px;">Need {max(0, 3 - entry["history_weeks"])} more week(s) of data</div>'
        else:
            delta = r1(proj - cur) if cur is not None else None
            good = (delta or 0) >= 0 if higher_is_better else (delta or 0) <= 0
            color = "#22C55E" if good else "#EF4444"
            arrow = "▲" if (delta or 0) > 0 else ("▼" if (delta or 0) < 0 else "→")
            body = f'<div class="predict-arrow" style="color:{color};">{cur} → {proj} {arrow}</div>'
        cards.append(f'''      <div class="predict-card">
        <div class="metric-label" style="color:#888;font-size:10px;font-family:monospace;text-transform:uppercase;letter-spacing:0.1em;">{label}</div>
        {body}
      </div>''')
    return "\n".join(cards)


def build_full_report_html(averages, trends, ai_analysis, history, rows, activities, start: date, end: date) -> str:
    trend_labels = [h["week_end"] for h in history] + [end.isoformat()]
    trend_labels_display = [date.fromisoformat(d).strftime("%b %d") for d in trend_labels]
    trend_series = {}
    for key, *_ in TREND_METRICS:
        series = [h["averages"].get(key) for h in history if isinstance(h.get("averages", {}).get(key), (int, float))]
        if trends[key]["history_weeks"] >= 2:
            trend_series[key] = series + ([averages.get(key)] if isinstance(averages.get(key), (int, float)) else [])

    html = DASHBOARD_TEMPLATE
    replacements = {
        "__PAGE_TITLE__": f"Weekly Report — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}",
        "__WEEK_END__": end.strftime("%B %d, %Y"),
        "__HISTORY_WEEKS__": str(len(history)),
        "__WEEK_HEADLINE__": ai_analysis["week_headline"],
        "__METRIC_CARDS__": _build_metric_cards_html(trends, averages, ai_analysis["metrics"]),
        "__COACH_TAKE__": ai_analysis["coach_take"],
        "__ACTIONS_HTML__": _build_actions_html(ai_analysis["top_actions"]),
        "__TREND_CHARTS_HTML__": _build_trend_charts_html(trends),
        "__NEXT_WEEK_OUTLOOK__": ai_analysis["next_week_outlook"],
        "__PREDICTION_CARDS__": _build_prediction_cards(trends),
        "__ACTIVITIES_ROWS__": _build_activities_rows(activities),
        "__GENERATED_AT__": end.isoformat(),
        "__DAILY_CSV_JSON__": json.dumps(daily_to_csv(rows)),
        "__ACTIVITIES_CSV_JSON__": json.dumps(activities_to_csv(activities)),
        "__TREND_LABELS_JSON__": json.dumps(trend_labels_display),
        "__TREND_SERIES_JSON__": json.dumps(trend_series),
    }
    for token, value in replacements.items():
        html = html.replace(token, value)
    return html


def save_report_html(html: str) -> None:
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(html)


# ---------- highlights email — lean, visual, no attachments ----------

def build_highlights_email(averages, trends, ai_analysis, rows, start: date, end: date) -> str:
    chart_url = build_email_chart_url(rows)

    tiles = []
    tile_defs = [("sleep_score", "Sleep Score", ""), ("overnight_hrv", "HRV", "ms"), ("resting_hr", "Resting HR", "bpm"), ("weekly_intensity_minutes", "Intensity Mins", "")]
    for key, label, unit in tile_defs:
        entry = trends[key]
        val = entry["current"] if entry["current"] is not None else "—"
        delta = entry["delta_vs_last_week"]
        if delta is None:
            delta_html = '<span style="color:#555;">first week logged</span>'
        else:
            good = (delta > 0) == entry["higher_is_better"] if delta != 0 else None
            color = "#888" if delta == 0 else ("#22C55E" if good else "#EF4444")
            arrow = "→" if delta == 0 else ("▲" if delta > 0 else "▼")
            delta_html = f'<span style="color:{color};">{arrow} {abs(delta)}</span>'
        tiles.append(f"""        <td width="25%" style="padding:10px 6px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:#fff;">{val}<span style="font-size:10px;color:#666;"> {unit}</span></div>
          <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:0.06em;font-family:monospace;margin:2px 0;">{label}</div>
          <div style="font-size:10px;font-family:monospace;">{delta_html}</div>
        </td>""")
    tiles_html = "\n".join(tiles)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="background-color:#050505;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#fff;margin:0;padding:20px 0;">
  <div style="max-width:520px;margin:0 auto;background-color:#0D0D0D;border:1px solid #1C1C1C;border-radius:16px;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#22C55E,#10B981,#3B82F6);"></div>
    <div style="padding:28px 28px 18px;">
      <span style="display:inline-block;background-color:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.3);padding:5px 12px;border-radius:20px;font-size:10px;color:#22C55E;font-weight:bold;letter-spacing:0.15em;text-transform:uppercase;font-family:monospace;">Weekly Report</span>
      <h1 style="font-size:20px;font-weight:800;letter-spacing:-0.02em;margin:12px 0 4px;text-transform:uppercase;">{start.strftime('%b %d')} — {end.strftime('%b %d, %Y')}</h1>
      <p style="font-size:14px;line-height:1.5;color:#D1D5DB;margin:10px 0 0;">{ai_analysis['week_headline']}</p>
    </div>
    <div style="background-color:#050505;border-top:1px solid #1C1C1C;border-bottom:1px solid #1C1C1C;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
        <tr>
{tiles_html}
        </tr>
      </table>
    </div>
    <div style="padding:0;text-align:center;background-color:#050505;">
      <img src="{chart_url}" alt="Sleep &amp; HRV" style="max-width:100%;display:block;border:none;">
    </div>
    <div style="padding:22px 28px;border-top:1px solid #1C1C1C;">
      <div style="border-left:3px solid #22C55E;padding-left:14px;">
        <p style="font-size:13.5px;line-height:1.5;color:#EEEEEE;font-style:italic;margin:0;">"{ai_analysis['coach_take']}"</p>
      </div>
    </div>
    <div style="padding:8px 28px 32px;text-align:center;">
      <a href="{REPORT_URL}" target="_blank" style="display:block;background-color:#22C55E;color:#000 !important;text-decoration:none;padding:15px 20px;border-radius:10px;font-weight:800;font-size:12px;letter-spacing:0.12em;text-transform:uppercase;font-family:monospace;">Open Interactive Dashboard &gt;</a>
      <p style="font-size:11px;color:#555;margin:14px 0 0;">Trends, predictions, per-metric coaching &amp; workout logs live there. CSVs are on the dashboard if you need the raw numbers.</p>
    </div>
  </div>
</body>
</html>"""


def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env("GMAIL_USER")
    msg["To"] = env("RECIPIENT_EMAIL")
    msg.attach(MIMEText("Your weekly Garmin report is ready. Open in an HTML-capable email client, or visit the dashboard link.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(env("GMAIL_USER"), env("GMAIL_APP_PASSWORD"))
        smtp.sendmail(env("GMAIL_USER"), env("RECIPIENT_EMAIL"), msg.as_string())


# ---------- main ----------

def main():
    end = date.today()
    start = end - timedelta(days=6)
    print(f"Pulling Garmin data {start} -> {end}")

    client = login()
    rows, activities = pull_week(client, end)
    averages = compute_averages(rows)

    history = load_history(before_date=end)
    print(f"Loaded {len(history)} prior week(s) of history for comparison")
    trends = compute_metric_trends(history, averages)

    context = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "averages": averages,
        "trends": {k: {kk: vv for kk, vv in v.items() if kk != "label"} for k, v in trends.items()},
        "daily_breakdown": [
            {
                "date": r["date"], "weekday": r["weekday"], "sleep_score": r.get("sleep_score"),
                "resting_hr": r.get("resting_hr"), "hrv": daily_hrv(r),
                "stress": r.get("avg_stress"), "steps": r.get("steps"),
            }
            for r in rows
        ],
        "activities": activities,
    }

    print("Running Gemini coaching analysis...")
    ai_analysis = get_gemini_analysis(context)

    # Save this week's snapshot for future weeks' comparisons/predictions.
    report_data = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "daily_data": rows,
        "activities": activities,
        "averages": averages,
        "ai_analysis": ai_analysis,
    }
    save_report_json(report_data, end)

    full_html = build_full_report_html(averages, trends, ai_analysis, history, rows, activities, start, end)
    save_report_html(full_html)
    print("Interactive dashboard saved to report.html")

    highlights_html = build_highlights_email(averages, trends, ai_analysis, rows, start, end)
    subject = f"🏃 Weekly Report — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"
    send_email(subject, highlights_html)
    print("Highlights email sent.")


if __name__ == "__main__":
    main()
