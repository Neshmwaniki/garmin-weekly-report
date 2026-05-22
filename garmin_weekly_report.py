"""
Garmin Weekly Report — pulls the last 7 days of health/activity data from
Garmin Connect, runs it through Gemini for a Whoop-style analysis, and emails
the full report to you. Runs every Sunday evening via GitHub Actions.
"""

import csv
import io
import os
import smtplib
import sys
import time
import json
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

def env(name, default=None):
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

# ---------- data summary formatter ----------

def build_data_summary(rows, activities):
    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def total(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) if vals else None

    # Keep and format clean activities structure
    parsed_activities = []
    if activities:
        for act in activities:
            parsed_activities.append({
                "startTimeLocal": act.get("startTimeLocal"),
                "activityName": act.get("activityName"),
                "distance": round(act.get("distance", 0) / 1000.0, 2) if act.get("distance") else 0, # km
                "duration": round(act.get("duration", 0) / 60.0, 1) if act.get("duration") else 0,   # minutes
                "averageHR": act.get("averageHR"),
                "maxHR": act.get("maxHR"),
                "calories": act.get("calories"),
                "averageSpeed": round(act.get("averageSpeed", 0) * 3.6, 1) if act.get("averageSpeed") else 0 # km/h
            })

    summary = {
        "week_start": rows[0]["date"],
        "week_end": rows[-1]["date"],
        "daily_data": rows,
        "activities": parsed_activities,
        "averages": {
            "sleep_score": avg("sleep_score"),
            "sleep_duration": seconds_to_hm(avg("sleep_seconds")),
            "deep_sleep": seconds_to_hm(avg("deep_seconds")),
            "rem_sleep": seconds_to_hm(avg("rem_seconds")),
            "resting_hr": avg("resting_hr"),
            "overnight_hrv": avg("avg_overnight_hrv"),
            "steps": avg("steps"),
            "avg_stress": avg("avg_stress"),
            "body_battery_high": avg("body_battery_high"),
            "body_battery_low": avg("body_battery_low"),
            "hrv_weekly_avg": avg("hrv_weekly_avg"),
            "hrv_status": rows[-1].get("hrv_status", "N/A"),
            "weekly_intensity_minutes": total("intensity_minutes"),
            "training_readiness": rows[-1].get("training_readiness", "N/A")
        }
    }
    return summary

# ---------- Gemini analysis ----------

ANALYSIS_PROMPT = """You are a personal health coach. Analyze the Garmin Connect fitness data below and generate a professional, structured weekly health report.
You MUST return ONLY a valid, raw JSON object (with no markdown block wrapper, no extra comment text, no formatting fences) following this schema:
{{
  "summary": "A concise 2-3 sentence visual summary paragraph overlooking their weekly recovery quality...",
  "sleep": "A concise analysis of their sleep score, cycles, overnight HRV, and sleep trends...",
  "activity": "A concise breakdown of steps, intensity minutes, strain trends, and movement consistency...",
  "stress": "A concise analysis of physical stress thresholds, body battery cycles, and readiness indexes...",
  "actions": [
    "Action 1 (bold title with colon): One specific numeric insight and action detail.",
    "Action 2 (bold title with colon): Another specific action item detail."
  ],
  "coach_take": "Your final executive coach takeaways. Warm, clinical, with a laser focus guidance."
}}

Data:
{data}
"""

def get_gemini_analysis(data_summary_text):
    """Call Gemini API using a fallback list of available modern models to guarantee execution."""
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    
    models_to_try = [
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash"
    ]
    
    last_exc = None
    for model_name in models_to_try:
        print(f"Trying model: {model_name}...")
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=ANALYSIS_PROMPT.format(data=data_summary_text),
                )
                text = response.text.strip()
                if text.startswith("```"):
                    if text.startswith("```json"):
                        text = text.split("```json", 1)[-1]
                    else:
                        text = text.split("```", 1)[-1]
                    text = text.rsplit("```", 1)[0].strip()
                print(f"Success with model {model_name}!")
                return text
            except Exception as e:
                last_exc = e
                err_str = str(e)
                if "429" in err_str or "quota" in err_str.lower() or "exhausted" in err_str.lower() or "ResourceExhausted" in err_str:
                    if attempt < 1:
                        wait = 35
                        print(f"Quota error for {model_name} (attempt {attempt+1}). Retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        print(f"Quota error for {model_name} exhausted all attempts for this model.")
                elif "404" in err_str or "not found" in err_str.lower() or "unsupported" in err_str.lower():
                    print(f"Model {model_name} is not available (404/not supported). Trying next model...")
                    break
                else:
                    print(f"Unexpected error for {model_name}: {e}. Trying next model...")
                    break
                    
    raise last_exc

# ---------- email ----------

HTML_EMAIL_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ background-color: #0A0A0A; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; color: #FFFFFF; margin: 0; padding: 40px 20px; }}
    .container {{ max-width: 600px; margin: 0 auto; background-color: #141414; border: 1px solid #262626; border-radius: 16px; padding: 40px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}
    h1 {{ font-size: 26px; font-weight: bold; font-family: "Space Grotesk", sans-serif; letter-spacing: -0.030em; margin: 0 0 8px 0; color: #FFFFFF; line-height: 1.2; }}
    .badge {{ display: inline-block; background-color: #0F1C15; border: 1px solid #164E2F; padding: 4px 12px; border-radius: 20px; font-size: 10px; color: #22C55E; font-weight: bold; letter-spacing: 0.20em; text-transform: uppercase; margin-bottom: 24px; }}
    .card {{ background-color: #0A0A0A; border: 1px solid #262626; border-radius: 12px; padding: 24px; margin-bottom: 32px; }}
    .summary-text {{ font-size: 15px; line-height: 1.6; color: #CCCCCC; margin: 0; }}
    .btn-container {{ text-align: center; margin: 40px 0; }}
    .btn {{ display: inline-block; background-color: #22C55E; color: #000000; text-decoration: none; padding: 16px 32px; font-size: 12px; font-weight: bold; letter-spacing: 0.150em; text-transform: uppercase; border-radius: 8px; box-shadow: 0 4px 12px rgba(34,197,94,0.2); font-family: "Space Grotesk", sans-serif; }}
    .btn:hover {{ background-color: #4ade80; }}
    .coach-section {{ border-top: 1px solid #262626; padding-top: 24px; margin-top: 32px; }}
    .coach-title {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.20em; color: #888888; font-weight: bold; margin-bottom: 8px; }}
    .coach-take {{ font-size: 13px; line-height: 1.5; color: #BFBFBF; font-style: italic; }}
    .footer {{ font-size: 10px; color: #444444; text-transform: uppercase; letter-spacing: 0.150em; border-top: 1px solid #262626; padding-top: 16px; margin-top: 40px; text-align: left; }}
  </style>
</head>
<body>
  <div class="container">
    <span class="badge">Performance Report Calibrated</span>
    <h1>Weekly Recovery & Performance Insights Are Ready</h1>
    <p style="color: #888; font-size: 12px; margin-top: 0; margin-bottom: 24px;">Week ending {end_date}</p>
    
    <div class="card">
      <p class="summary-text">
        {summary_text}
      </p>
    </div>

    <div class="coach-section" style="margin-bottom: 32px;">
      <div class="coach-title">Daily Coach Takeaway</div>
      <div class="coach-take">"{coach_take}"</div>
    </div>

    <div class="btn-container">
      <a href="{dashboard_link}" target="_blank" class="btn">Launch Design Dashboard</a>
    </div>

    <div class="footer">
      Device: Garmin Connect &bull; Workflow: weekly-report.yml &bull; Generated: {end_date}
    </div>
  </div>
</body>
</html>"""

def send_email(subject, html_body):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = env("GMAIL_USER")
    msg["To"] = env("RECIPIENT_EMAIL")

    msg.attach(MIMEText(html_body, "html"))

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

    # Build the full structured JSON report
    report_data = build_data_summary(rows, activities)
    
    # We turn the averages structure into text for the Gemini prompt
    prompt_data = json.dumps({
        "averages": report_data["averages"],
        "daily_scores_and_hrv": [{"date": r["date"], "sleep_score": r.get("sleep_score"), "hrv": r.get("avg_overnight_hrv")} for r in rows],
        "activities_count": len(activities)
    }, indent=2)

    print("Running Gemini analysis...")
    analysis_json_str = get_gemini_analysis(prompt_data)

    # Parse and merge Gemini's structured analysis
    try:
        ai_analysis = json.loads(analysis_json_str)
        report_data["ai_analysis"] = ai_analysis
    except Exception as e:
        print(f"JSON analysis parsing failed: {e}. Raw response: {analysis_json_str}")
        report_data["ai_analysis"] = {
            "summary": "Garmin data and metrics compiled successfully.",
            "sleep": "Review sleep details on the live interactive performance monitor dashboard.",
            "activity": "Review step averages and activity details on the dashboard.",
            "stress": "Review stress logs and body battery averages.",
            "actions": ["Prioritize optimal resting hours and monitor HRV baseline."],
            "coach_take": "Sync data and review detailed visual logs in the performance dashboard."
        }

    # Save the structured dashboard JSON file locally into "reports" folder so GitHub actions commits it
    os.makedirs("reports", exist_ok=True)
    report_filename = f"reports/report_{end.isoformat()}.json"
    with open(report_filename, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)
    print(f"Saved structured report to {report_filename}")

    # Build dynamic dashboard url pointing to our Cloud Run container
    # Fallback to hardcoded URL if APP_URL is not set as env var
    app_url = env("APP_URL", "https://ais-pre-wqay2bw7mgjjc6347awiso-43870111567.europe-west1.run.app")
    dashboard_link = f"{app_url}/?report={end.isoformat()}"

    # Build custom email HTML body
    summary_text = report_data["ai_analysis"].get("summary", "")
    coach_take = report_data["ai_analysis"].get("coach_take", "")
    html_body = HTML_EMAIL_TEMPLATE.format(
        summary_text=summary_text,
        coach_take=coach_take,
        dashboard_link=dashboard_link,
        end_date=end.isoformat()
    )

    subject = f"Garmin Weekly Performance Summary — week ending {end.isoformat()}"
    send_email(subject, html_body)
    print("Polished summary report email sent successfully!")

if __name__ == "__main__":
    main()
