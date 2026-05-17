"""
Garmin Weekly Report — pulls the last 7 days of health/activity data from
Garmin Connect, runs it through Claude for a Whoop-style analysis, and emails
the full report to you. Runs every Sunday evening via GitHub Actions.

Required environment variables (set as GitHub Actions Secrets):
    GARMIN_EMAIL         — your Garmin Connect login email
    GARMIN_PASSWORD      — your Garmin Connect password
    GMAIL_USER           — gmail address that sends the report
    GMAIL_APP_PASSWORD   — 16-char Google app password (NOT your real password)
    RECIPIENT_EMAIL      — where the report gets sent (can equal GMAIL_USER)
    GEMINI_API_KEY       — Google Gemini API key for the Whoop-style analysis (free tier)
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


def get_gemini_analysis(data_summary: str) -> str:
    genai.configure(api_key=env("GEMINI_API_KEY"))
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(ANALYSIS_PROMPT.format(data=data_summary))
    # Strip markdown code fences if Gemini wraps the HTML
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    return text


# ---------- email ----------

def build_html_email(analysis_html: str, start: date, end: date) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 