"""
Garmin Weekly Report — pulls the last 7 days of health/activity data from
Garmin Connect, runs it through Gemini for a Whoop-style analysis, and emails
the full report to you. Runs every Sunday evening via GitHub Actions.

Required environment variables (set as GitHub Actions Secrets):
    GARMIN_EMAIL        — your Garmin Connect login email
    GARMIN_PASSWORD     — your Garmin Connect password
    GMAIL_USER          — gmail address that sends the report
    GMAIL_APP_PASSWORD  — 16-char Google app password (NOT your real password)
    RECIPIENT_EMAIL     — where the report gets sent (can equal GMAIL_USER)
    GEMINI_API_KEY      — Google Gemini API key (free tier)
"""

import csv
import io
import os
import smtplib
import sys
import time
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
    """Login to Garmin Connect with retry on rate-limit errors."""
    last_exc = None
    for attempt in range(3):
        try:
            client = Garmin(env("GARMIN_EMAIL"), env("GARMIN_PASSWORD"))
            client.login()
            return client
        except GarminConnectTooManyRequestsError as e:
            last_exc = e
            wait = 30 * (attempt + 1)
            print(f"Garmin login rate-limited (attempt {attempt+1}). Waiting {wait}s...")
            time.sleep(wait)
        except Exception as e:
            last_exc = e
            if "429" in str(e) or "rate" in str(e).lower():
                wait = 30 * (attempt + 1)
                print(f"Garmin login 429 error (attempt {attempt+1}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise last_exc

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

    return rows

# ---------- CSV helpers ----------

def daily_to_csv(rows):
    if not rows:
        return ""
    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()

def activities_to_csv(activities):
    if not activities:
        return ""
    keep = ["startTimeLocal", "activityName", "distance", "duration",
            "averageHR", "maxHR", "calories", "averageSpeed"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keep, extrasaction="ignore")
    w.writeheader()
    w.writerows(activities)
    return buf.getvalue()

# ---------- summary builder ----------

def build_data_summary(rows, activities):
    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def total(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) if vals else None

    lines = [
        f"Week: {rows[0]['date']} to {rows[-1]['date']}",
        "",
        "=== DAILY DATA ===",
        daily_to_csv(rows),
        "",
        "=== ACTIVITIES ===",
        activities_to_csv(activities) if activities else "(none recorded)",
        "",
        "=== WEEKLY AVERAGES ===",
        f"Sleep score: {avg('sleep_score')}",
        f"Sleep duration: {seconds_to_hm(avg('sleep_seconds'))}",
        f"Deep sleep: {seconds_to_hm(avg('deep_seconds'))}",
        f"REM sleep: {seconds_to_hm(avg('rem_seconds'))}",
        f"Overnight HRV: {avg('avg_overnight_hrv')}",
        f"HRV status: {rows[-1].get('hrv_status', 'N/A')}",
        f"Resting HR: {avg('resting_hr')}",
        f"Steps/day: {avg('steps')}",
        f"Avg stress: {avg('avg_stress')}",
        f"Body battery high: {avg('body_battery_high')}",
        f"Body battery low: {avg('body_battery_low')}",
        f"Weekly intensity minutes: {total('intensity_minutes')}",
        f"Training readiness (latest): {rows[-1].get('training_readiness', 'N/A')}",
    ]
    return "\n".join(lines)

# ---------- Gemini analysis ----------

ANALYSIS_PROMPT = """You are a personal health coach. Analyse the Garmin data below and produce a concise, Whoop-style weekly health report in HTML format.

Data:
{data}

Return ONLY an HTML fragment (no <!DOCTYPE>, no <html>/<head>/<body> tags) with these sections:

<h2>Weekly Summary</h2>
<p>[2-3 sentence overview of the week. Be direct.]</p>

<h2>Sleep</h2>
<p>[Analysis of sleep quality, duration, HRV, and recovery.]</p>

<h2>Activity and Strain</h2>
<p>[Analysis of steps, intensity minutes, activities, and overall strain.]</p>

<h2>Stress and Recovery</h2>
<p>[Analysis of stress levels, body battery, and readiness scores.]</p>

<h2>Top Actions This Week</h2>
<ol>
  <li><strong>[Action title]:</strong> [What data shows + one concrete action. 2 sentences.]</li>
</ol>

<h2>Coach Take</h2>
<p>[2-3 sentences. Most important thing to work on and why. Direct and warm.]</p>

Rules: energetic and direct, reference specific numbers, honest but encouraging, no fluff.
Output only the HTML fragment above, no markdown fences, no extra commentary."""


def get_gemini_analysis(data_summary):
    """Call Gemini API using the new google-genai SDK with retry on quota errors."""
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    try:
        print("Listing models...")
        for m in client.models.list():
            print(f"  - {m.name}")
    except Exception as le:
        print("List models error:", le)

    last_exc = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=ANALYSIS_PROMPT.format(data=data_summary),
            )
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                text = text.rsplit("```", 1)[0].strip()
            return text
        except Exception as e:
            last_exc = e
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower() or "exhausted" in err_str.lower() or "ResourceExhausted" in err_str:
                if attempt < 2:
                    wait = 65
                    print(f"Gemini quota error (attempt {attempt+1}): {e}. Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"Gemini quota error (attempt {attempt+1}): {e}. All retries exhausted.")
            else:
                raise
    raise last_exc

# ---------- email ----------

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 680px; margin: auto; color: #222; }}
    h1   {{ background: #1a1a2e; color: #fff; padding: 20px; border-radius: 8px 8px 0 0; margin-bottom: 0; }}
    h2   {{ color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 4px; }}
    .meta {{ background: #f5f5f5; padding: 10px 20px; font-size: 13px; color: #555; }}
    .body {{ padding: 20px; }}
    ol   {{ padding-left: 20px; }}
    li   {{ margin-bottom: 8px; }}
  </style>
</head>
<body>
  <h1>Garmin Weekly Report</h1>
  <div class="meta">Week ending {end_date}</div>
  <div class="body">
    {analysis}
  </div>
</body>
</html>"""


def build_html_email(analysis_html, end_date):
    return HTML_TEMPLATE.format(analysis=analysis_html, end_date=end_date)


def send_email(subject, html_body, csv_attachment, attachment_name):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = env("GMAIL_USER")
    msg["To"] = env("RECIPIENT_EMAIL")

    msg.attach(MIMEText(html_body, "html"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(csv_attachment.encode())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
    msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(env("GMAIL_USER"), env("GMAIL_APP_PASSWORD"))
        s.sendmail(env("GMAIL_USER"), env("RECIPIENT_EMAIL"), msg.as_string())


# ---------- main ----------

def main():
    end = date.today()
    start = end - timedelta(days=6)
    print(f"Pulling Garmin data {start} -> {end}")

    client = login()

    rows = pull_week(client, end)

    try:
        activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    except Exception as e:
        print(f"Activities pull failed: {e}")
        activities = []

    data_summary = build_data_summary(rows, activities)
    daily_csv = daily_to_csv(rows)

    print("Running Gemini analysis...")
    analysis_html = get_gemini_analysis(data_summary)

    html_body = build_html_email(analysis_html, end.isoformat())

    subject = f"Garmin Weekly Report — week ending {end.isoformat()}"
    send_email(subject, html_body, daily_csv, f"garmin_week_{end.isoformat()}.csv")
    print("Report sent successfully!")


if __name__ == "__main__":
    main()
