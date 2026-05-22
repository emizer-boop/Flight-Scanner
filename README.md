# Skyscanner Daily Tracker (GitHub Actions worker)

A fully automated cloud worker. Runs once a day on GitHub's servers, hits the sky-scrapper API, and posts the results as a new GitHub Issue you can scroll like a feed. No laptop, no cron, no Terminal after setup.

It tracks:

- **Price drops** on routes seen before (default 10% threshold)
- **New destinations** discovered from your origin airports
- **Cheap weekend deals** (Fri to Sun) under your price cap

## One-time setup (about 3 minutes)

### 1. Create a private GitHub repo

Go to <https://github.com/new>, name it `skyscanner-tracker`, set it to **Private**, click Create.

### 2. Upload these files

On the empty repo page, click **uploading an existing file**. Drag every file from this folder (including the hidden `.github/` folder). Commit.

If you prefer Terminal:

```bash
cd ~/Downloads
unzip skyscanner-tracker.zip
cd skyscanner-tracker
git init
git remote add origin https://github.com/YOUR_USERNAME/skyscanner-tracker.git
git add .
git commit -m "initial"
git branch -M main
git push -u origin main
```

### 3. Add your API key as a secret

In the repo: **Settings -> Secrets and variables -> Actions -> New repository secret**

- Name: `RAPIDAPI_KEY`
- Value: your RapidAPI key (rotate the one you sent in chat first)

Click **Add secret**.

### 4. Allow Actions to write

In the repo: **Settings -> Actions -> General -> Workflow permissions**, choose **Read and write permissions**, save.

### 5. Run it once to confirm

**Actions** tab -> **Daily Skyscanner Tracker** -> **Run workflow** -> green Run workflow button.

After about 1-2 minutes a new Issue appears in your **Issues** tab titled `Skyscanner feed - YYYY-MM-DD`. That is your feed.

From now on, GitHub runs it every day at **8:00 AM ET** automatically.

## How the feed works

Each run creates a fresh GitHub Issue containing:

```
## Price drops
- JFK to LIS | 2026-06-12 to 2026-06-17 | $612 to $498 (18.6% off)

## New destinations discovered
- EWR to RAK | from $451 | 2026-07-04 to 2026-07-11

## Cheap weekend deals (under $400)
- LGA to MIA | 2026-06-05 to 2026-06-07 | $187
```

The Issues page becomes your scrollable historical feed. You can subscribe to email notifications on the repo (Watch -> Custom -> Issues) and you'll get a daily email automatically.

## Configure what to track

Edit `config.json` in the repo (web UI works - click the file, click the pencil):

```json
{
  "origins": ["JFK", "LGA", "EWR"],
  "trip_lengths_days": [3, 5, 7],
  "look_ahead_days": [14, 30, 60, 90],
  "price_drop_pct": 10.0,
  "weekend_deal_usd": 400
}
```

Commit. Next run uses the new config.

## Changing the schedule

Edit `.github/workflows/daily.yml`, line `- cron: "0 12 * * *"`. The time is in UTC. Examples:

- `0 12 * * *` = 8 AM ET (EDT) / 7 AM ET (EST)
- `0 13 * * *` = 9 AM ET (EDT)
- `0 11 * * 1-5` = 7 AM ET weekdays only

## Optional: also run locally

If you ever want to run it on your laptop, the original CLI still works:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your key
python tracker.py
```

## Files

```
skyscanner-tracker/
├── .github/workflows/daily.yml   # the worker (GitHub Actions cron)
├── tracker.py                    # the script
├── config.json                   # routes / thresholds
├── requirements.txt
├── .env.example                  # only needed for local runs
├── .gitignore
├── history.db                    # auto-created, committed back each run
└── reports/                      # auto-created, one .md per day
```

## Notes

- sky-scrapper is a third-party RapidAPI scraper of Skyscanner. If a field stops parsing, check `parse_result` in `tracker.py`.
- GitHub Actions gives 2,000 free minutes/month on private repos; this worker uses about 2 minutes/day = 60/month.
- The price history (`history.db`) is committed back to your repo each run so day-over-day comparisons survive.
