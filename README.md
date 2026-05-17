# Garmin Weekly Report → Email

Pulls 7 days of Garmin Connect data every Sunday at 7pm Nairobi time (EAT) and emails it to you as a markdown summary + CSV attachments. Forward the markdown to Claude and ask for a Whoop-style report on what you did well and where to improve.

## What's included in the email

- **weekly_summary.md** — weekly averages + daily breakdown table + activities table
- **daily_metrics.csv** — raw daily numbers (sleep stages, HRV, RHR, stress, Body Battery, steps, intensity minutes, training readiness)
- **activities.csv** — every recorded workout/run with distance, duration, HR

## One-time setup

### 1. Create the repo
1. Make a **private** GitHub repo (e.g. `garmin-weekly-report`).
2. Push these files to it: `garmin_weekly_report.py`, `requirements.txt`, `.github/workflows/weekly-report.yml`.

### 2. Generate a Gmail App Password
You can't use your real Gmail password — Google blocks that. You need a 16-character app password.

1. Go to https://myaccount.google.com/security
2. Make sure **2-Step Verification** is ON (required to create app passwords).
3. Go to https://myaccount.google.com/apppasswords
4. Create a new app password named "Garmin Report". Copy the 16 characters (no spaces).

### 3. Add GitHub Actions secrets
In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add these five:

| Secret name | Value |
|---|---|
| `GARMIN_EMAIL` | your Garmin Connect login email |
| `GARMIN_PASSWORD` | your Garmin Connect password |
| `GMAIL_USER` | the Gmail address that sends the report |
| `GMAIL_APP_PASSWORD` | the 16-char app password from step 2 |
| `RECIPIENT_EMAIL` | where to send the report (can equal `GMAIL_USER`) |

### 4. Test it
- Go to **Actions** tab → **Garmin Weekly Report** → **Run workflow**. This triggers it manually so you don't have to wait until Sunday.
- Check your inbox in ~1–2 minutes. Also check the Actions log if anything fails.

After that, it runs automatically every Sunday at 19:00 EAT (16:00 UTC).

## Using the report with Claude

When the email arrives, open the markdown attachment, paste it into a new Claude chat, and say:

> Here's my weekly Garmin data. Give me a Whoop-style summary: what I did well this week, and the main areas to improve including how. Be specific and direct.

## Notes & limits

- **Garmin login is fragile.** Garmin occasionally blocks programmatic logins. If you get auth errors, log into Garmin Connect in a browser once to clear any captcha, then re-run.
- **MFA on Garmin** will break this. If you have it enabled, the `garminconnect` library supports it but needs a different login flow — let me know and I'll adjust.
- **Schedule timing isn't perfectly precise.** GitHub Actions cron can run a few minutes late under load. If exact-to-the-minute matters, run on a personal server with cron instead.
- **Data freshness** depends on when your watch last synced. Make sure the Garmin app on your phone has synced before the report runs.
