"""
Garmin Weekly Report — pulls the last 7 days of health/activity data from
Garmin Connect, formats it into a clean markdown + CSV summary, and emails
it to you. Designed to run on a Sunday evening schedule via GitHub Actions.

Required environment variables (set as GitHub Actions Secrets):
    GARMIN_EMAIL         — your Garmin Connect login email
    GARMIN_PASSWORD      — your Garmin Connect password
    GMAIL_USER           — gmail address that sends the report
    GMAIL_APP_PASSWORD   — 16-char Google app password (NOT your real password)
    RECIPIENT_EMAIL      — where the report gets sent (can equal GMAIL_USER)
"""

import csv
import io
import os
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage

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
    """Walk a nested dict safely; return default if any key is missing/None."""
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
    """Pull 7 days ending on `end` (inclusive)."""
    days = [end - timedelta(days=i) for i in range(6, -1, -1)]
    rows = []

    for d in days:
        iso = d.isoformat()
        row = {"date": iso, "weekday": d.strftime("%a")}

        # --- Sleep ---
        try:
            sleep = client.get_sleep_data(iso)
            dto = safe_get(sleep, "dailySleepDTO", default={})
            row["sleep_score"] = safe_get(dto, "sleepScores", "overall", "value")
            row["sleep_seconds"] = safe_get(dto, "sleepTimeSeconds")
            row["deep_seconds"] = safe_get(dto, "deepSleepSeconds")
            row["light_seconds"] = safe_get(dto, "lightSleepSeconds")
            row["rem_seconds"] = safe_get(dto, "remSleepSeconds")
            row["awake_seconds"] = safe_get(dto, "awakeSleepSeconds")
            row["avg_overnight_hr"] = safe_get(dto, "averageSpO2HRSleep") or safe_get(
                sleep, "restingHeartRate"
            )
            row["avg_overnight_hrv"] = safe_get(dto, "avgOvernightHrv")
        except Exception as e:
            print(f"  sleep pull failed for {iso}: {e}")

        # --- Steps / stress / Body Battery / RHR via daily summary ---
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

        # --- HRV status ---
        try:
            hrv = client.get_hrv_data(iso)
            row["hrv_weekly_avg"] = safe_get(hrv, "hrvSummary", "weeklyAvg")
            row["hrv_status"] = safe_get(hrv, "hrvSummary", "status")
        except Exception as e:
            print(f"  hrv pull failed for {iso}: {e}")

        # --- Training readiness (if available) ---
        try:
            readiness = client.get_training_readiness(iso)
            if readiness and isinstance(readiness, list) and readiness:
                row["training_readiness"] = readiness[0].get("score")
        except Exception as e:
            print(f"  readiness pull failed for {iso}: {e}")

        rows.append(row)

    # --- Activities for the week ---
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


# ---------- formatting ----------

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


def build_markdown_summary(rows, activities, start: date, end: date) -> str:
    def avg(field, scale=1):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals) / scale, 1) if vals else None

    def total(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return sum(vals) if vals else 0

    md = []
    md.append(f"# Garmin Weekly Report — {start.isoformat()} to {end.isoformat()}\n")
    md.append("## Weekly Averages\n")
    md.append(f"- **Sleep score (avg):** {avg('sleep_score')}")
    md.append(f"- **Sleep duration (avg):** {seconds_to_hm(avg('sleep_seconds'))}")
    md.append(f"- **Resting HR (avg):** {avg('resting_hr')} bpm")
    md.append(f"- **HRV weekly avg:** {avg('hrv_weekly_avg')} ms")
    md.append(f"- **Stress (avg):** {avg('avg_stress')}")
    md.append(f"- **Steps (avg):** {avg('steps')}")
    md.append(f"- **Steps (total):** {total('steps'):,}")
    md.append(f"- **Intensity minutes (total):** {total('intensity_minutes')}\n")

    md.append("## Daily Breakdown\n")
    md.append("| Day | Date | Sleep | Score | RHR | HRV | Stress | Steps | BB High/Low |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        md.append(
            f"| {r['weekday']} | {r['date']} | "
            f"{seconds_to_hm(r.get('sleep_seconds'))} | "
            f"{r.get('sleep_score') or '—'} | "
            f"{r.get('resting_hr') or '—'} | "
            f"{r.get('avg_overnight_hrv') or r.get('hrv_weekly_avg') or '—'} | "
            f"{r.get('avg_stress') or '—'} | "
            f"{r.get('steps') or '—'} | "
            f"{r.get('body_battery_high') or '—'}/{r.get('body_battery_low') or '—'} |"
        )

    md.append("\n## Activities\n")
    if not activities:
        md.append("_No activities recorded._")
    else:
        md.append("| Date | Type | Distance (km) | Duration (min) | Avg HR | Max HR |")
        md.append("|---|---|---|---|---|---|")
        for a in activities:
            md.append(
                f"| {a['date']} | {a.get('type') or '—'} | "
                f"{a.get('distance_km') or '—'} | "
                f"{a.get('duration_min') or '—'} | "
                f"{a.get('avg_hr') or '—'} | "
                f"{a.get('max_hr') or '—'} |"
            )

    md.append(
        "\n---\n*Forward this to Claude and ask for a Whoop-style summary: "
        "what you did well + main areas to improve and how.*"
    )
    return "\n".join(md)


# ---------- email ----------

def send_email(subject, body, attachments):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env("GMAIL_USER")
    msg["To"] = env("RECIPIENT_EMAIL")
    msg.set_content(body)

    for filename, content in attachments:
        msg.add_attachment(
            content.encode("utf-8"),
            maintype="text",
            subtype="csv" if filename.endswith(".csv") else "markdown",
            filename=filename,
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(env("GMAIL_USER"), env("GMAIL_APP_PASSWORD"))
        smtp.send_message(msg)


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
    markdown = build_markdown_summary(rows, activities, start, end)
    daily_csv = daily_to_csv(rows)
    activities_csv = activities_to_csv(activities)

    subject = f"Garmin Weekly Report — {start} to {end}"
    body = (
        "Your weekly Garmin data is attached.\n\n"
        "Forward the markdown to Claude and ask for the Whoop-style report:\n"
        "  \"Here's my weekly Garmin data — give me a summary of what I did well "
        "and the main areas to improve, including how.\"\n\n"
        "Summary preview:\n\n" + markdown[:1500] + "\n..."
    )

    send_email(
        subject,
        body,
        attachments=[
            ("weekly_summary.md", markdown),
            ("daily_metrics.csv", daily_csv),
            ("activities.csv", activities_csv),
        ],
    )
    print("Email sent.")


if __name__ == "__main__":
    main()
