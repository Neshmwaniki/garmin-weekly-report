"""
Garmin Weekly Report — pulls the last 7 days of health/activity data from
Garmin Connect, runs it through Gemini for a Whoop-style analysis, and emails
the full report to you. Runs every Sunday evening via GitHub Actions.

Required environment variables (set as GitHub Actions Secrets):
    GARMIN_EMAIL         — your Garmin Connect login email
    GARMIN_PASSWORD      — your Garmin Connect password
    GMAIL_USER           — gmail address that sends the report
    GMAIL_APP_PASSWORD   — 16-char Google app password (NOT your real password)
    RECIPIENT_EMAIL      — where the report gets sent (can equal GMAIL_USER)
    GEMINI_API_KEY       — Google Gemini API key (free tier)
"""

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

import google.generativeai as genai
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


# ---------- helpers ----------

def env(name):
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

def login():
    client = Garmin(env("GARMIN_EMAIL"), env("GARMIN_PASSWORD"))
    client.login()
    return client


def pull_week(client, end):
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

def daily_to_csv(rows):
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def activities_to_csv(acts):
    if not acts:
        return "no activities recorded\n"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(acts[0].keys()))
    writer.writeheader()
    writer.writerows(acts)
    return buf.getvalue()


# ---------- data summary for Gemini ----------

def build_data_summary(rows, activities, start, end):
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


# ---------- Gemini analysis ----------

ANALYSIS_PROMPT = """You are an elite performance coach writing a weekly health debrief.
The athlete uses a Garmin watch. Here is their raw data for the past 7 days:

{data}

Write a punchy, Whoop-style weekly report in HTML. Use exactly this structure:

<h2>Week in One Line</h2>
<p><strong>[One bold sentence capturing the overall shape of the week. Be specific.]</strong></p>

<h2>What You Crushed</h2>
<ul>
  <li><strong>[Win title]:</strong> [1-2 sentences referencing actual numbers.]</li>
  [3 wins total]
</ul>

<h2>Numbers at a Glance</h2>
<table>
  <tr><th>Metric</th><th>This Week</th><th>What It Means</th></tr>
  <tr><td>Sleep Score</td><td>[value]</td><td>[short phrase]</td></tr>
  <tr><td>HRV</td><td>[value] ms</td><td>[short phrase]</td></tr>
  <tr><td>Resting HR</td><td>[value] bpm</td><td>[short phrase]</td></tr>
  <tr><td>Intensity Minutes</td><td>[value]</td><td>[short phrase]</td></tr>
</table>

<h2>5 Focus Areas for Next Week</h2>
<ol>
  <li><strong>[Action title]:</strong> [What data shows + one concrete action. 2 sentences.]</li>
  [5 items total]
</ol>

<h2>Coach's Take</h2>
<p>[2-3 sentences. Most important thing to work on and why. Direct and warm.]</p>

Rules: energetic and direct, reference specific numbers, honest but encouraging, no fluff.
Output only the HTML fragment above — no markdown fences, no extra commentary."""


import time

def get_gemini_analysis(data_summary):
    genai.configure(api_key=env("GEMINI_API_KEY"))
    model = genai.GenerativeModel("gemini-2.0-flash-lite")
    for attempt in range(3):
        try:
            response = model.generate_content(ANALYSIS_PROMPT.format(data=data_summary))
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                text = text.rsplit("```", 1)[0].strip()
            return text
        except Exception as e:
            if attempt < 2:
                print(f"Gemini API error (attempt {attempt+1}): {e}. Retrying in 60s...")
                time.sleep(60)
            else:
                raise


# ---------- email ----------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 620px; margin: 0 auto; padding: 20px;
    color: #1a1a1a; background: #f4f4f5;
  }
  .header {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    color: white; border-radius: 14px;
    padding: 28px 32px; margin-bottom: 16px;
  }
  .header h1 { margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px; }
  .header p { margin: 6px 0 0; font-size: 13px; color: #93c5fd; }
  .card {
    background: white; border-radius: 14px; padding: 28px 32px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07); margin-bottom: 12px;
  }
  h2 {
    font-size: 16px; font-weight: 700; margin: 20px 0 12px;
    padding-bottom: 8px; border-bottom: 2px solid #f1f5f9; color: #0f172a;
  }
  h2:first-child { margin-top: 0; }
  p { font-size: 14px; line-height: 1.65; margin: 0 0 12px; }
  ul, ol { padding-left: 20px; margin: 0 0 12px; }
  li { margin-bottom: 10px; line-height: 1.55; font-size: 14px; }
  strong { color: #0f172a; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 4px 0 12px; }
  th {
    background: #f8fafc; text-align: left; padding: 9px 12px;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    color: #64748b; border-bottom: 1px solid #e2e8f0;
  }
  td { padding: 10px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: top; }
  td:nth-child(2) { font-weight: 700; color: #0f172a; }
  td:nth-child(3) { color: #64748b; font-size: 12px; }
  .footer { font-size: 11px; color: #94a3b8; text-align: center; padding: 12px; }
</style>
</head>
<body>
  <div class="header">
    <h1>&#127939; Weekly Performance Report</h1>
    <p>{date_range}</p>
  </div>
  <div class="card">
    {analysis}
  </div>
  <div class="footer">
    Raw data attached as CSV &nbsp;&middot;&nbsp; Runs automatically every Sunday at 7 PM EAT
  </div>
</body>
</html>"""


def build_html_email(analysis_html, start, end):
    date_range = f"{start.strftime('%B %d')} — {end.strftime('%B %d, %Y')}"
    return HTML_TEMPLATE.format(date_range=date_range, analysis=analysis_html)


def send_email(subject, html_body, attachments):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = env("GMAIL_USER")
    msg["To"] = env("RECIPIENT_EMAIL")

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Your weekly Garmin performance report is ready.", "plain"))
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
    print(f"Pulling Garmin data {start} -> {end}")

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

    html_body = build_html_email(analysis_html, start, end)
    subject = f"\U0001f3c3 Weekly Report — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"

    send_email(
        subject,
        html_body,
        attachments=[
            ("daily_metrics.csv", daily_csv),
            ("activities.csv", activities_csv),
        ],
    )
    print("Email sent.")


if __name__ == "__main__":
    main()
