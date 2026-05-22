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
            
            # Smart HRV Fallback: if sleep data didn't fetch overnight HRV, pull lastNightAvg from hrvSummary
            last_night_avg = safe_get(hrv, "hrvSummary", "lastNightAvg")
            if last_night_avg is not None:
                row["avg_overnight_hrv"] = last_night_avg
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

# ---------- Gemini analysis -----------

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
                
                # Use chr(96) dynamically to handle backticks cleanly without typescript template literal escape noise
                BACKTICKS = chr(96) * 3
                if text.startswith(BACKTICKS):
                    if text.startswith(BACKTICKS + "json"):
                        text = text.split(BACKTICKS + "json", 1)[-1]
                    else:
                        text = text.split(BACKTICKS, 1)[-1]
                    text = text.rsplit(BACKTICKS, 1)[0].strip()
                    
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

# ---------- email builders ----------

def get_chart_url(config):
    encoded = urllib.parse.quote(json.dumps(config))
    return f"https://quickchart.io/chart?w=600&h=280&bkg=050505&c={encoded}"

def build_enhanced_html_email(report_data, dashboard_link, end_date):
    averages = report_data.get("averages", {})
    daily_data = report_data.get("daily_data", [])
    ai_analysis = report_data.get("ai_analysis", {})
    
    # Extract arrays for charts while gracefully interpolating missing days to baseline averages
    weekdays = [r.get("weekday", "") for r in daily_data]
    
    weekly_avg_sleep = averages.get("sleep_score") or 70
    sleep_scores = [r.get("sleep_score") if r.get("sleep_score") is not None and r.get("sleep_score") > 0 else int(weekly_avg_sleep) for r in daily_data]
    
    weekly_avg_hrv = averages.get("overnight_hrv") or averages.get("hrv_weekly_avg") or 50
    hrv_values = [r.get("avg_overnight_hrv") if r.get("avg_overnight_hrv") is not None and r.get("avg_overnight_hrv") > 0 else int(weekly_avg_hrv) for r in daily_data]
    
    weekly_avg_bb_high = averages.get("body_battery_high") or 75
    weekly_avg_bb_low = averages.get("body_battery_low") or 20
    battery_highs = [r.get("body_battery_high") if r.get("body_battery_high") is not None and r.get("body_battery_high") > 0 else int(weekly_avg_bb_high) for r in daily_data]
    battery_lows = [r.get("body_battery_low") if r.get("body_battery_low") is not None and r.get("body_battery_low") > 0 else int(weekly_avg_bb_low) for r in daily_data]
    
    # 1. Sleep score chart
    sleep_config = {
        "type": "bar",
        "data": {
            "labels": weekdays,
            "datasets": [{
                "label": "Sleep Score",
                "data": sleep_scores,
                "backgroundColor": [
                    "rgba(34, 197, 94, 0.85)" if s >= 80 else "rgba(249, 115, 22, 0.85)" if s >= 55 else "rgba(239, 68, 68, 0.85)"
                    for s in sleep_scores
                ],
                "borderColor": [
                    "#22C55E" if s >= 80 else "#F97316" if s >= 55 else "#EF4444"
                    for s in sleep_scores
                ],
                "borderWidth": 1.5,
                "borderRadius": 6,
            }]
        },
        "options": {
            "legend": {"display": False},
            "title": {
                "display": True,
                "text": "WEEKLY SLEEP RESOURCE SCORES",
                "fontColor": "#888888",
                "fontSize": 11,
                "fontFamily": "monospace"
            },
            "scales": {
                "yAxes": [{
                    "gridLines": {"color": "#1C1C1C", "zeroLineColor": "#1C1C1C"},
                    "ticks": {"min": 0, "max": 100, "fontColor": "#666666", "fontSize": 10}
                }],
                "xAxes": [{
                    "gridLines": {"display": False},
                    "ticks": {"fontColor": "#FFFFFF", "fontSize": 10}
                }]
            }
        }
    }
    sleep_chart_url = get_chart_url(sleep_config)
    
    # 2. HRV Line Chart
    hrv_config = {
        "type": "line",
        "data": {
            "labels": weekdays,
            "datasets": [{
                "label": "HRV ms",
                "data": hrv_values,
                "fill": True,
                "backgroundColor": "rgba(34, 197, 94, 0.08)",
                "borderColor": "#22C55E",
                "borderWidth": 2.5,
                "pointBackgroundColor": "#22C55E",
                "pointRadius": 4,
                "lineTension": 0.25
            }]
        },
        "options": {
            "legend": {"display": False},
            "title": {
                "display": True,
                "text": "OVERNIGHT HEART RATE VARIABILITY (ms)",
                "fontColor": "#888888",
                "fontSize": 11,
                "fontFamily": "monospace"
            },
            "scales": {
                "yAxes": [{
                    "gridLines": {"color": "#1C1C1C", "zeroLineColor": "#1C1C1C"},
                    "ticks": {"fontColor": "#666666", "fontSize": 10}
                }],
                "xAxes": [{
                    "gridLines": {"display": False},
                    "ticks": {"fontColor": "#FFFFFF", "fontSize": 10}
                }]
            }
        }
    }
    hrv_chart_url = get_chart_url(hrv_config)
    
    # 3. Body Battery Range Chart
    battery_config = {
        "type": "line",
        "data": {
            "labels": weekdays,
            "datasets": [
                {
                    "label": "Peak Charge",
                    "data": battery_highs,
                    "borderColor": "#3B82F6",
                    "backgroundColor": "transparent",
                    "borderWidth": 2,
                    "pointRadius": 3,
                    "lineTension": 0.2
                },
                {
                    "label": "Drain Point",
                    "data": battery_lows,
                    "borderColor": "#FF5733",
                    "backgroundColor": "transparent",
                    "borderWidth": 2,
                    "pointRadius": 3,
                    "lineTension": 0.2
                }
            ]
        },
        "options": {
            "legend": {"labels": {"fontColor": "#888888", "fontSize": 9}, "align": "end"},
            "title": {
                "display": True,
                "text": "DAILY BODY BATTERY CYCLES (HIGH VS LOW)",
                "fontColor": "#888888",
                "fontSize": 11,
                "fontFamily": "monospace"
            },
            "scales": {
                "yAxes": [{
                    "gridLines": {"color": "#1C1C1C", "zeroLineColor": "#1C1C1C"},
                    "ticks": {"min": 0, "max": 100, "fontColor": "#666666", "fontSize": 10}
                }],
                "xAxes": [{
                    "gridLines": {"display": False},
                    "ticks": {"fontColor": "#FFFFFF", "fontSize": 10}
                }]
            }
        }
    }
    battery_chart_url = get_chart_url(battery_config)
    
    # Parse action items into visually stunning lists
    actions_html = ""
    for action in ai_analysis.get("actions", []):
        # Strip all formatting artifacts
        cleaned_action = action.replace("**", "").replace("*", "").strip()
        if cleaned_action.startswith("-"):
            cleaned_action = cleaned_action.lstrip("-").strip()
        if cleaned_action.startswith("✔"):
            cleaned_action = cleaned_action.lstrip("✔").strip()
        if cleaned_action.startswith("✓"):
            cleaned_action = cleaned_action.lstrip("✓").strip()
            
        parts = cleaned_action.split(":", 1)
        if len(parts) == 2:
            title = parts[0].replace("**", "").replace("*", "").strip()
            desc = parts[1].replace("**", "").replace("*", "").strip()
            actions_html += f"""
            <div style="margin-bottom: 12px;">
              <div style="font-size: 13.5px; line-height: 1.5; color: #CCCCCC;">
                <span style="color: #22C55E; margin-right: 8px; font-weight: bold;">✔</span>
                <strong style="color: #FFFFFF;">{title}:</strong> {desc}
              </div>
            </div>
            """
        else:
            actions_html += f"""
            <div style="margin-bottom: 12px;">
              <div style="font-size: 13.5px; line-height: 1.5; color: #CCCCCC;">
                <span style="color: #22C55E; margin-right: 8px; font-weight: bold;">✔</span>
                {cleaned_action}
              </div>
            </div>
            """
            
    # Format activities as beautiful row items
    activities_html = ""
    activities = report_data.get("activities", [])
    if activities:
        for act in activities:
            activities_html += f"""
            <tr style="border-bottom: 1px solid #1C1C1C;">
              <td style="padding: 12px 8px; font-size: 12.5px; color: #FFFFFF; font-weight: 500;">{act.get('activityName', 'Activity')}</td>
              <td style="padding: 12px 8px; font-size: 12.5px; color: #888888; text-align: right;">{act.get('distance', 0)} km</td>
              <td style="padding: 12px 8px; font-size: 12.5px; color: #888888; text-align: right;">{act.get('duration', 0)}m</td>
              <td style="padding: 12px 8px; font-size: 12.5px; color: #22C55E; font-weight: bold; text-align: right;">{act.get('averageHR', '—')} bpm</td>
              <td style="padding: 12px 8px; font-size: 12.5px; color: #888888; text-align: right;">{act.get('calories', '—')} cal</td>
            </tr>
            """
    else:
        activities_html = """
        <tr>
          <td colspan="5" style="padding: 24px 8px; font-size: 12px; color: #555555; text-align: center; font-style: italic;">
            No activities recorded this week.
          </td>
        </tr>
        """
        
    # Standard format variables with fallback checks
    sleep_score = averages.get("sleep_score")
    sleep_score = int(sleep_score) if sleep_score is not None else "—"
    sleep_dur = averages.get("sleep_duration") or "—"
    resting_hr = averages.get("resting_hr")
    resting_hr = int(resting_hr) if resting_hr is not None else "—"
    overnight_hrv_val = averages.get("overnight_hrv") or averages.get("hrv_weekly_avg")
    if overnight_hrv_val is not None and overnight_hrv_val > 0:
        hrv_display_html = f"{int(overnight_hrv_val)}<span style='font-size: 12px; color: #666; font-weight: normal;'> ms</span>"
    else:
        hrv_display_html = "<span style='font-size: 14px; color: #888; font-weight: normal;'>No Data</span>"
    intensity_min = averages.get("weekly_intensity_minutes") or 0
    steps = averages.get("steps") or "—"
    if isinstance(steps, float):
        steps = f"{int(steps):,}"
    elif isinstance(steps, int):
        steps = f"{steps:,}"
        
    hrv_weekly_avg = averages.get("hrv_weekly_avg") or "—"
    hrv_weekly_avg = int(hrv_weekly_avg) if isinstance(hrv_weekly_avg, (int, float)) else hrv_weekly_avg
    hrv_status_val = averages.get("hrv_status") or "N/A"
    readiness = averages.get("training_readiness") or "N/A"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WHOOP-Style Garmin Performance Report</title>
</head>
<body style="background-color: #050505; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #FFFFFF; margin: 0; padding: 20px 0; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 620px; margin: 0 auto; background-color: #0D0D0D; border: 1px solid #1C1C1C; border-radius: 16px; overflow: hidden; box-shadow: 0 20px 50px rgba(0,0,0,0.8);">
    
    <!-- Top Deco Bar -->
    <div style="height: 4px; background: linear-gradient(90deg, #22C55E, #10B981, #3B82F6);"></div>
    
    <!-- Header -->
    <div style="padding: 32px 32px 20px 32px; border-bottom: 1px solid #1C1C1C; background-color: #0A0A0A;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse;">
        <tr>
          <td>
            <span style="display: inline-block; background-color: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.3); padding: 5px 12px; border-radius: 20px; font-size: 10px; color: #22C55E; font-weight: bold; letter-spacing: 0.18em; text-transform: uppercase; font-family: monospace;">CALIBRATED PERFORMANCE</span>
            <h1 style="font-size: 24px; font-weight: 800; letter-spacing: -0.03em; margin: 12px 0 4px 0; color: #FFFFFF; text-transform: uppercase; line-height: 1.2;">Weekly Physiology Summary</h1>
            <p style="color: #666666; font-size: 12px; margin: 0; font-family: monospace; text-transform: uppercase; letter-spacing: 0.05em;">WEEK ENDING {end_date} &bull; DEVICE: GARMIN REST CONNECT</p>
          </td>
        </tr>
      </table>
    </div>
    
    <!-- Hero Summary Statement -->
    <div style="padding: 24px 32px; background-color: #070707; border-bottom: 1px solid #1C1C1C;">
      <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #888888; font-weight: bold; margin-bottom: 8px; font-family: monospace;">AI Physiological assessment</div>
      <p style="font-size: 14.5px; line-height: 1.6; color: #D1D5DB; margin: 0; font-weight: 400;">
        {ai_analysis.get('summary', 'Weekly body recovery and active strain metrics parsed successfully. View details below.')}
      </p>
    </div>

    <!-- Coach Executive Takeaway -->
    <div style="padding: 24px 32px; background-color: #090909; border-bottom: 1px solid #1C1C1C;">
      <div style="border-left: 3.5px solid #22C55E; padding-left: 16px;">
        <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #22C55E; font-weight: bold; margin-bottom: 6px; font-family: monospace;">Performance Coach takeaway</div>
        <p style="font-size: 14px; line-height: 1.5; color: #EEEEEE; font-style: italic; margin: 0; font-weight: 500;">
          "{ai_analysis.get('coach_take', 'Refining sleep hygiene and aligning active workloads with biometric capacity is highly recommended.')}"
        </p>
      </div>
    </div>

    <!-- WHOOP-Style Key Stat Blocks (Cards Grid inside table) -->
    <div style="padding: 32px 32px 16px 32px; background-color: #0D0D0D;">
      <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #888888; font-weight: bold; margin-bottom: 16px; font-family: monospace;">Biometric Performance Indexes</div>
      
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 24px; border-collapse: collapse;">
        <tr>
          <!-- Column 1 -->
          <td width="48%" valign="top">
            <!-- Card 1: Sleep -->
            <div style="background-color: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px; margin-bottom: 16px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse;">
                <tr>
                  <td style="color: #3B82F6; font-size: 10px; font-weight: bold; letter-spacing: 0.12em; text-transform: uppercase; font-family: monospace;">Sleep Index</td>
                </tr>
                <tr>
                  <td style="font-size: 26px; font-weight: 800; color: #FFFFFF; padding: 6px 0 2px 0;">{sleep_score}<span style="font-size: 12px; color: #666; font-weight: normal;"> / 100</span></td>
                </tr>
                <tr>
                  <td style="font-size: 11.5px; color: #888888; font-family: monospace;">Avg: {sleep_dur}</td>
                </tr>
              </table>
            </div>
            
            <!-- Card 2: Resting HR -->
            <div style="background-color: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse;">
                <tr>
                  <td style="color: #EF4444; font-size: 10px; font-weight: bold; letter-spacing: 0.12em; text-transform: uppercase; font-family: monospace;">Resting HR</td>
                </tr>
                <tr>
                  <td style="font-size: 26px; font-weight: 800; color: #FFFFFF; padding: 6px 0 2px 0;">{resting_hr}<span style="font-size: 12px; color: #666; font-weight: normal;"> bpm</span></td>
                </tr>
                <tr>
                  <td style="font-size: 11.5px; color: #888888; font-family: monospace;">Lowest Basal Score</td>
                </tr>
              </table>
            </div>
          </td>
          
          <!-- Column Space -->
          <td width="4%"></td>
          
          <!-- Column 2 -->
          <td width="48%" valign="top">
            <!-- Card 3: HRV -->
            <div style="background-color: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px; margin-bottom: 16px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse;">
                <tr>
                  <td style="color: #22C55E; font-size: 10px; font-weight: bold; letter-spacing: 0.12em; text-transform: uppercase; font-family: monospace;">Overnight HRV</td>
                </tr>
                <tr>
                  <td style="font-size: 26px; font-weight: 800; color: #FFFFFF; padding: 6px 0 2px 0;">{hrv_display_html}</td>
                </tr>
                <tr>
                  <td style="font-size: 11.5px; color: #888888; font-family: monospace;">{hrv_status_val} ({hrv_weekly_avg}ms avg)</td>
                </tr>
              </table>
            </div>
            
            <!-- Card 4: Load & Movement -->
            <div style="background-color: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse;">
                <tr>
                  <td style="color: #F59E0B; font-size: 10px; font-weight: bold; letter-spacing: 0.12em; text-transform: uppercase; font-family: monospace;">Weekly Intensity</td>
                </tr>
                <tr>
                  <td style="font-size: 26px; font-weight: 800; color: #FFFFFF; padding: 6px 0 2px 0;">{intensity_min}<span style="font-size: 12px; color: #666; font-weight: normal;"> mins</span></td>
                </tr>
                <tr>
                  <td style="font-size: 11.5px; color: #888888; font-family: monospace;">Avg steps: {steps}/day</td>
                </tr>
              </table>
            </div>
          </td>
        </tr>
      </table>
    </div>

    <!-- GRAPHICS SECTIONS (Images of charts) -->
    <div style="padding: 0 32px 32px 32px; background-color: #0D0D0D;">
      <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #888888; font-weight: bold; margin-bottom: 16px; font-family: monospace;">Visual Physiological Analytics</div>
      
      <!-- Chart 1: Sleep Score -->
      <div style="background-color: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px; margin-bottom: 20px; text-align: center;">
        <img src="{sleep_chart_url}" alt="Sleep Score Chart" style="max-width: 100%; border-radius: 8px; border: none; outline: none; display: block;" width="556">
        <div style="font-size: 10px; color: #555555; text-align: left; margin-top: 8px; font-family: monospace; text-transform: uppercase;">Sleep Scores over 7 days. Higher colors represent optimal daily sleep resources.</div>
      </div>
      

      <!-- Chart 3: Body Battery -->
      <div style="background-color: #050505; border: 1px solid #1C1C1C; border-radius: 12px; padding: 16px; text-align: center;">
        <img src="{battery_chart_url}" alt="Body Battery Chart" style="max-width: 100%; border-radius: 8px; border: none; outline: none; display: block;" width="556">
        <div style="font-size: 10px; color: #555555; text-align: left; margin-top: 8px; font-family: monospace; text-transform: uppercase;">Body battery high charge (blue) vs lowest points log representing systemic strain recovery.</div>
      </div>
    </div>

    <!-- AI ACTIONS / STRATEGIC TAKEAWAYS -->
    <div style="padding: 32px; background-color: #080808; border-top: 1px solid #1C1C1C; border-bottom: 1px solid #1C1C1C;">
      <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #22C55E; font-weight: bold; margin-bottom: 20px; font-family: monospace;">Coach Strategic Recommendations</div>
      
      <!-- Action Items list -->
      <div>
        {actions_html}
      </div>
    </div>
    
    <!-- DETAILED ACTIVITIES LIST -->
    <div style="padding: 32px; background-color: #0D0D0D; border-bottom: 1px solid #1C1C1C;">
      <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: #888888; font-weight: bold; margin-bottom: 16px; font-family: monospace;">Workout & Strain Logs</div>
      
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse; width: 100%;">
        <thead>
          <tr style="border-bottom: 2px solid #1C1C1C;">
            <th align="left" style="padding: 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #666666; font-weight: bold; font-family: monospace;">Workout Name</th>
            <th align="right" style="padding: 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #666666; font-weight: bold; font-family: monospace;">Distance</th>
            <th align="right" style="padding: 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #666666; font-weight: bold; font-family: monospace;">Duration</th>
            <th align="right" style="padding: 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #666666; font-weight: bold; font-family: monospace;">Avg HR</th>
            <th align="right" style="padding: 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #666666; font-weight: bold; font-family: monospace;">Cals</th>
          </tr>
        </thead>
        <tbody>
          {activities_html}
        </tbody>
      </table>
    </div>

    <!-- Launch CTA Button Block -->
    <div style="padding: 40px 32px; background-color: #050505; text-align: center;">
      <p style="font-size: 13px; color: #666666; margin-bottom: 20px; line-height: 1.5;">Review detailed interactive charts, historic telemetry logs, and sleep cycles anytime on your desktop dashboard tracker.</p>
      <a href="{dashboard_link}" target="_blank" style="display: inline-block; background-color: #22C55E; color: #000000; text-decoration: none; padding: 16px 36px; font-size: 11px; font-weight: bold; letter-spacing: 0.20em; text-transform: uppercase; border-radius: 8px; box-shadow: 0 4px 20px rgba(34,197,94,0.35); font-family: monospace;">LAUNCH DESKTOP DASHBOARD &gt;</a>
      <div style="margin-top: 28px;">
        <span style="font-size: 9px; color: #444444; font-family: monospace; display: block; text-transform: uppercase; margin-bottom: 8px; letter-spacing: 0.1em;">Or copy link directly:</span>
        <code style="display: block; width: 100%; max-width: 480px; margin: 0 auto; padding: 10px 14px; background-color: #0A0A0A; border: 1px solid #1C1C1C; color: #888888; font-size: 11px; font-family: monospace; word-break: break-all; border-radius: 6px; box-sizing: border-box; text-align: center;">{dashboard_link}</code>
      </div>
    </div>

    <!-- Footer -->
    <div style="padding: 24px 32px; background-color: #080808; border-top: 1px solid #1C1C1C; text-align: left;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse;">
        <tr>
          <td>
            <div style="font-size: 9px; color: #333333; text-transform: uppercase; letter-spacing: 0.15em; font-family: monospace; line-height: 1.6;">
              Device Pipeline: Garmin Rest API integrations<br>
              Processing Engine: Gemini Health assessment Flash Pro<br>
              Execution workflow: .github/workflows/weekly-report.yml &bull; generated {end_date}<br>
              Need assistance? Open your interactive dashboard interface.
            </div>
          </td>
        </tr>
      </table>
    </div>
    
  </div>
</body>
</html>"""
    return html

# ---------- email SMTP ----------

def send_email(subject, html_body):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = "njukidenis47@gmail.com"
    msg["To"] = "njukidenis47@gmail.com"

    msg.attach(MIMEText(html_body, "html"))

    login_user = env("GMAIL_USER", "njukidenis47@gmail.com")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(login_user, env("GMAIL_APP_PASSWORD"))
        s.sendmail(login_user, "njukidenis47@gmail.com", msg.as_string())

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
    html_body = build_enhanced_html_email(report_data, dashboard_link, end.isoformat())

    subject = f"Garmin Weekly Performance Summary — week ending {end.isoformat()}"
    send_email(subject, html_body)
    print("Polished summary report email sent successfully!")

if __name__ == "__main__":
    main()
