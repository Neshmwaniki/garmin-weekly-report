"""
Garmin Weekly Report — pulls the last 7 days of health/activity data from
Garmin Connect, runs it through Gemini for a Whoop-style analysis, publishes
the full report to GitHub Pages, and emails you a highlights summary with a link.
Runs every Sunday evening via GitHub Actions.

Required environment variables (set as GitHub Actions Secrets):
    GARMIN_EMAIL         — your Garmin Connect login email
    GARMIN_PASSWORD      — your Garmin Connect password
    GMAIL_USER           — gmail address that sends the report
    GMAIL_APP_PASSWORD   — 16-char Google app password (NOT your real password)
    RECIPIENT_EMAIL      — where the report gets sent (can equal GMAIL_USER)
    GEMINI_API_KEY       — Google Gemini API key for the Whoop-style analysis (free tier)
"""

REPORT_URL = "https://neshmwaniki.github.io/garmin-weekly-report/"

import csv
import io
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from google import genai
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


# ---------- helpers ----------

def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
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


# ---------- Garmin pulls ----------

def login() -> Garmin:
    client = Garmin(env("GARMIN_EMAIL"), env("GARMIN_PASSWORD"))
    client.login()
    return client


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


# ---------- CSV helpers ----------

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


# ---------- data summary for Claude ----------

def build_data_summary(rows, activities, start: date, end: date) -> str:
    def avg(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def total(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return sum(vals) if vals else 0

    lines = [f"Week: {start.isoformat()} to {end.isoformat()}\n"]
    lines.append("WEEKLY AVERAGES")
    lines.append(f"Sleep score: {avg('sleep_score')}")
    lines.append(f"Sleep duration: {seconds_to_hm(avg('sleep_seconds'))}")
    lines.append(f"Resting HR: {avg('resting_hr')} bpm")
    lines.append(f"HRV weekly avg: {avg('hrv_weekly_avg')} ms")
    lines.append(f"Stress: {avg('avg_stress')}")
    lines.append(f"Steps/day: {avg('steps')}")
    lines.append(f"Total steps: {total('steps'):,}")
    lines.append(f"Total intensity minutes: {total('intensity_minutes')}\n")

    lines.append("DAILY BREAKDOWN")
    lines.append("Day | Date | Sleep | Score | RHR | HRV | Stress | Steps | BB High/Low")
    for r in rows:
        lines.append(
            f"{r['weekday']} | {r['date']} | "
            f"{seconds_to_hm(r.get('sleep_seconds'))} | "
            f"{r.get('sleep_score') or '—'} | "
            f"{r.get('resting_hr') or '—'} | "
            f"{r.get('avg_overnight_hrv') or r.get('hrv_weekly_avg') or '—'} | "
            f"{r.get('avg_stress') or '—'} | "
            f"{r.get('steps') or '—'} | "
            f"{r.get('body_battery_high') or '—'}/{r.get('body_battery_low') or '—'}"
        )

    lines.append("\nACTIVITIES")
    if not activities:
        lines.append("No activities recorded.")
    else:
        for a in activities:
            dist = f"{a['distance_km']} km" if a.get("distance_km") else ""
            lines.append(
                f"{a['date']} — {a.get('type', '—')} | "
                f"{dist} | {a.get('duration_min')} min | "
                f"Avg HR {a.get('avg_hr') or '—'} | Max HR {a.get('max_hr') or '—'}"
            )

    return "\n".join(lines)


# ---------- Claude analysis ----------

ANALYSIS_PROMPT = """You are an elite performance coach writing a weekly health debrief.
The athlete uses a Garmin watch. Here is their raw data for the past 7 days:

{data}

Write a punchy, Whoop-style weekly report in HTML. Use exactly this structure:

<h2>📋 Week in One Line</h2>
<p><strong>[One bold sentence that captures the overall shape of the week — training load, recovery quality, standout moments. Be specific, not generic.]</strong></p>

<h2>🏆 What You Crushed</h2>
<ul>
  <li><strong>[Win title]:</strong> [1–2 sentences referencing the actual number and why it matters.]</li>
  <li>... (3 wins total)</li>
</ul>

<h2>📊 Numbers at a Glance</h2>
<table>
  <tr><th>Metric</th><th>This Week</th><th>What It Means</th></tr>
  <tr><td>Sleep Score</td><td>[value]</td><td>[1 short phrase]</td></tr>
  <tr><td>HRV</td><td>[value] ms</td><td>[1 short phrase]</td></tr>
  <tr><td>Resting HR</td><td>[value] bpm</td><td>[1 short phrase]</td></tr>
  <tr><td>Intensity Minutes</td><td>[value]</td><td>[1 short phrase]</td></tr>
</table>

<h2>🎯 5 Focus Areas for Next Week</h2>
<ol>
  <li><strong>[Action-oriented title]:</strong> [What the data shows + one specific, concrete action. 2 sentences max.]</li>
  ... (5 items total)
</ol>

<h2>💡 Coach's Take</h2>
<p>[2–3 sentences. The single most important thing to work on and why — written like a coach who genuinely cares. Direct, warm, no fluff.]</p>

Tone rules:
- Energetic and direct, like Whoop or a great personal trainer
- Always reference specific numbers to make it feel personal
- Honest about weak spots but frame improvements as opportunities, not failures
- No corporate language, no "it is important to note", no hedging
- Keep HTML clean — only use the tags shown above
"""


# Tried in order; gemini-1.5-flash (the old default here) was retired in Sept 2025.
# Keeping a short fallback list so a single model retirement doesn't break the
# whole pipeline again — if the first one 404s/is unavailable, we try the next.
GEMINI_MODEL_FALLBACKS = ["gemini-3.7-flash", "gemini-2.5-flash"]


def get_gemini_analysis(data_summary: str) -> str:
    client = genai.Client(api_key=env("GEMINI_API_KEY"))

    last_error = None
    for model_name in GEMINI_MODEL_FALLBACKS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=ANALYSIS_PROMPT.format(data=data_summary),
            )
            text = response.text.strip()
            # Strip markdown code fences if Gemini wraps the HTML
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                text = text.rsplit("```", 1)[0].strip()
            return text
        except Exception as e:
            print(f"  Gemini model '{model_name}' failed: {e}")
            last_error = e

    sys.exit(f"All Gemini models failed. Last error: {last_error}")


# ---------- email ----------

def build_full_report_html(analysis_html: str, start: date, end: date) -> str:
    """Full report page — saved to report.html and deployed to GitHub Pages."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Weekly Report — {start.strftime("%b %d")} to {end.strftime("%b %d, %Y")}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 680px; margin: 0 auto; padding: 24px 20px;
    color: #1a1a1a; background: #f4f4f5;
  }}
  .header {{
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    color: white; border-radius: 14px;
    padding: 28px 32px; margin-bottom: 16px;
  }}
  .header h1 {{ margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px; }}
  .header p {{ margin: 6px 0 0; font-size: 13px; color: #93c5fd; }}
  .card {{
    background: white; border-radius: 14px; padding: 28px 32px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07); margin-bottom: 12px;
  }}
  h2 {{
    font-size: 16px; font-weight: 700; margin: 0 0 14px;
    padding-bottom: 8px; border-bottom: 2px solid #f1f5f9;
    color: #0f172a;
  }}
  p {{ font-size: 14px; line-height: 1.65; margin: 0 0 12px; }}
  ul, ol {{ padding-left: 20px; margin: 0 0 12px; }}
  li {{ margin-bottom: 10px; line-height: 1.55; font-size: 14px; }}
  strong {{ color: #0f172a; }}
  table {{
    width: 100%; border-collapse: collapse;
    font-size: 13px; margin: 4px 0 12px;
  }}
  th {{
    background: #f8fafc; text-align: left; padding: 9px 12px;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    color: #64748b; border-bottom: 1px solid #e2e8f0;
  }}
  td {{
    padding: 10px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: top;
  }}
  td:nth-child(2) {{ font-weight: 700; color: #0f172a; }}
  td:nth-child(3) {{ color: #64748b; font-size: 12px; }}
  .footer {{
    font-size: 11px; color: #94a3b8; text-align: center; padding: 12px;
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>🏃 Weekly Performance Report</h1>
    <p>{start.strftime("%B %d")} — {end.strftime("%B %d, %Y")}</p>
  </div>
  <div class="card">
    {analysis_html}
  </div>
  <div class="footer">
    Generated by Garmin Connect · Updated every Sunday at 7 PM EAT
  </div>
</body>
</html>"""


def save_report_html(html: str) -> None:
    """Write the full report to report.html for GitHub Pages deployment."""
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(html)


def build_highlights_email(rows: list, activities: list, start: date, end: date) -> str:
    """Compact email: 6 key metrics + a link to the full report on GitHub Pages."""
    def avg(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def total(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return sum(vals) if vals else 0

    sleep_score = avg("sleep_score") or "—"
    hrv = avg("avg_overnight_hrv") or avg("hrv_weekly_avg") or "—"
    rhr = avg("resting_hr") or "—"
    intensity = total("intensity_minutes")
    steps_total = total("steps")
    total_steps = f"{int(steps_total):,}" if steps_total else "—"
    activity_count = len(activities)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 520px; margin: 0 auto; padding: 20px;
    color: #1a1a1a; background: #f4f4f5;
  }}
  .header {{
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    color: white; border-radius: 14px; padding: 24px 28px; margin-bottom: 12px;
  }}
  .header h1 {{ margin: 0; font-size: 20px; font-weight: 800; letter-spacing: -0.3px; }}
  .header p {{ margin: 5px 0 0; font-size: 12px; color: #93c5fd; }}
  .card {{
    background: white; border-radius: 14px; padding: 22px 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07); margin-bottom: 10px;
  }}
  .card-label {{
    font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: #94a3b8; margin: 0 0 16px;
  }}
  .metrics {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
  }}
  .metric {{ text-align: center; }}
  .metric .val {{ font-size: 26px; font-weight: 800; color: #0f172a; line-height: 1; }}
  .metric .lbl {{
    font-size: 10px; color: #94a3b8; text-transform: uppercase;
    letter-spacing: 0.05em; margin-top: 5px;
  }}
  .divider {{ border: none; border-top: 1px solid #f1f5f9; margin: 16px 0; }}
  .btn {{
    display: block;
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    color: white !important; text-align: center; padding: 15px 20px;
    border-radius: 10px; text-decoration: none;
    font-weight: 700; font-size: 15px; margin-bottom: 10px;
  }}
  .footer {{ font-size: 11px; color: #94a3b8; text-align: center; padding: 6px; }}
</style>
</head>
<body>
  <div class="header">
    <h1>🏃 Weekly Report</h1>
    <p>{start.strftime("%B %d")} — {end.strftime("%B %d, %Y")}</p>
  </div>
  <div class="card">
    <div class="card-label">This week at a glance</div>
    <div class="metrics">
      <div class="metric">
        <div class="val">{sleep_score}</div>
        <div class="lbl">Sleep Score</div>
      </div>
      <div class="metric">
        <div class="val">{hrv}</div>
        <div class="lbl">HRV (ms)</div>
      </div>
      <div class="metric">
        <div class="val">{rhr}</div>
        <div class="lbl">Resting HR</div>
      </div>
      <div class="metric">
        <div class="val">{intensity}</div>
        <div class="lbl">Intensity Mins</div>
      </div>
      <div class="metric">
        <div class="val">{total_steps}</div>
        <div class="lbl">Total Steps</div>
      </div>
      <div class="metric">
        <div class="val">{activity_count}</div>
        <div class="lbl">Activities</div>
      </div>
    </div>
  </div>
  <a href="{REPORT_URL}" class="btn">View Full Report →</a>
  <div class="footer">Full AI analysis · Updates every Sunday at 7 PM EAT</div>
</body>
</html>"""


def send_email(subject: str, html_body: str, attachments: list):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = env("GMAIL_USER")
    msg["To"] = env("RECIPIENT_EMAIL")

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Your weekly Garmin performance report is ready. Open in an HTML-capable email client to view.", "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    for filename, content in attachments:
        part = MIMEBase("text", "csv")
        part.set_payload(content.encode("utf-8"))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(env("GMAIL_USER"), env("GMAIL_APP_PASSWORD"))
        smtp.sendmail(env("GMAIL_USER"), env("RECIPIENT_EMAIL"), msg.as_string())


# ---------- main ----------

def main():
    end = date.today()
    start = end - timedelta(days=6)
    print(f"Pulling Garmin data {start} → {end}")

    try:
        client = login()
    except (GarminConnectAuthenticationError, GarminConnectConnectionError,
            GarminConnectTooManyRequestsError) as e:
        sys.exit(f"Garmin login failed: {e}")

    rows, activities = pull_week(client, end)
    data_summary = build_data_summary(rows, activities, start, end)
    daily_csv = daily_to_csv(rows)
    activities_csv = activities_to_csv(activities)

    print("Running Gemini analysis...")
    analysis_html = get_gemini_analysis(data_summary)

    # Save full report for GitHub Pages
    full_html = build_full_report_html(analysis_html, start, end)
    save_report_html(full_html)
    print("Full report saved to report.html")

    # Send compact highlights email with link
    highlights_html = build_highlights_email(rows, activities, start, end)
    subject = f"🏃 Weekly Report — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"

    send_email(
        subject,
        highlights_html,
        attachments=[
            ("daily_metrics.csv", daily_csv),
            ("activities.csv", activities_csv),
        ],
    )
    print("Email sent.")


if __name__ == "__main__":
    main()
