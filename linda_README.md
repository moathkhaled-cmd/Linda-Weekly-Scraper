# Linda Cars Scraper

Automatically scrapes all ads from [lindacars.com](https://www.lindacars.com), saves them to a dated CSV, and emails you a weekly report.

---

## What It Does

- Scrapes **all pages automatically** — starts at page 0, stops when no new ads are found (no manual page limit needed)
- Extracts per ad:
  - Make, Model, Trim, Color, Year, Mileage
  - Fuel type, Engine size, Transmission, Drive
  - Body Type, Seats, Condition, Accidents
  - Body / Mechanical / Interior / General condition ratings
  - Regional Specs, Emission Standard
  - Full-size Images (upgraded from thumbnail to `fit-1324xauto`)
- Saves results to `Linda Cars ad/YYYY-MM-DD.csv`
- Compares with the previous run and marks every ad:
  | Status | Meaning |
  |---|---|
  | **NEW** | First time this ad has been seen |
  | **UPDATED** | Price or mileage changed — shows old → new |
  | **UNCHANGED** | No changes since last run |
  | **REMOVED** | Was listed before, no longer found |
- Emails you the CSV with a summary automatically

---

## Folder Structure

```
linda-scraper/                        ← your GitHub repo root
├── .github/
│   └── workflows/
│       └── linda_weekly_scrape.yml   ← automation schedule (upload here)
├── scraper/
│   └── linda_scraper.py              ← the scraper (upload here)
└── README.md                         ← this file (optional, upload here)

Linda Cars ad/                        ← created automatically on first run
├── 2025-06-01.csv
├── 2025-06-08.csv
└── 2025-06-15.csv
```

---

## GitHub Upload Instructions (Step by Step)

### Step 1 — Create a new GitHub repo

1. Go to [github.com](https://github.com) and sign in
2. Click **+** (top right) → **New repository**
3. Name it `linda-scraper`, set to **Private**, click **Create repository**

---

### Step 2 — Upload the scraper file

1. In your new repo click **"creating a new file"** (or Add file → Create new file)
2. In the filename box type:
   ```
   scraper/linda_scraper.py
   ```
   GitHub creates the `scraper/` folder automatically when you type the `/`
3. Open `linda_scraper.py` in Notepad → **Ctrl+A** → **Ctrl+C**
4. Click inside the big text area on GitHub → **Ctrl+V**
5. Click **Commit new file**

---

### Step 3 — Upload the workflow file

> ⚠️ Must use "Create new file" (not upload) because `.github` is a hidden folder

1. Click **Add file** → **Create new file**
2. In the filename box type **exactly**:
   ```
   .github/workflows/linda_weekly_scrape.yml
   ```
3. Open `linda_weekly_scrape.yml` in Notepad → **Ctrl+A** → **Ctrl+C** → paste into GitHub
4. Click **Commit new file**

Your repo should now look like:
```
linda-scraper/
├── .github/workflows/linda_weekly_scrape.yml
└── scraper/linda_scraper.py
```

---

### Step 4 — Add email secrets

1. In your repo click **Settings** tab (top menu)
2. Left sidebar → **Secrets and variables** → **Actions**
3. Click **New repository secret** — add these three:

| Secret Name | Value |
|---|---|
| `EMAIL_SENDER` | your Gmail address e.g. `you@gmail.com` |
| `EMAIL_PASSWORD` | your 16-char Gmail App Password (see below) |
| `EMAIL_TO` | recipient emails, comma-separated, no spaces |

**How to get a Gmail App Password:**
1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Make sure **2-Step Verification** is ON
3. Search **"App passwords"** → create one for Mail
4. Use the 16-character code — **not** your real Gmail password

**EMAIL_TO with multiple recipients** (no quotes, no spaces):
```
alice@gmail.com,bob@gmail.com,charlie@gmail.com
```

---

### Step 5 — Test it manually

1. Click the **Actions** tab in your repo
2. Click **Linda Cars Weekly Scraper** in the left list
3. Click **Run workflow** → **Run workflow** (green button)
4. A run appears — click it to watch the live logs
5. Takes ~20–40 minutes. When done, check your email for the CSV

---

## Changing the Schedule

The schedule is in `.github/workflows/linda_weekly_scrape.yml`:

```yaml
on:
  schedule:
    - cron: '0 6 * * 1'   # Every Monday at 6:00 AM UTC
```

Edit the cron line to change when it runs:

| Cron | Meaning |
|---|---|
| `0 6 * * 1` | Every Monday 6:00 AM UTC |
| `0 6 * * 0` | Every Sunday 6:00 AM UTC |
| `0 8 * * 3` | Every Wednesday 8:00 AM UTC |
| `0 6 1 * *` | 1st of every month 6:00 AM UTC |
| `0 6 * * 1,4` | Every Monday & Thursday 6:00 AM UTC |

> **Dubai time (GST) = UTC + 4**
> So `0 6 * * 1` runs at **10:00 AM Dubai time** every Monday.
> For 8:00 AM Dubai use `0 4 * * 1`.

---

## Running Locally (on your PC)

### 1. Install dependencies
```bash
pip install selenium pandas
```
Make sure **Google Chrome** is installed.

### 2. Set your Windows username in the scraper

Open `linda_scraper.py` and find:
```python
CHROME_PROFILE = r"C:\Users\User\AppData\Local\Google\Chrome\User Data\Selenium_Linda"
```
Replace `User` with your actual Windows username, e.g. `Moath`.

### 3. Set your email in the scraper

Find these lines and replace the fallback values:
```python
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER",   "your_gmail@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "xxxx xxxx xxxx xxxx")
EMAIL_TO_RAW   = os.environ.get("EMAIL_TO",       "your_email@gmail.com")
```

### 4. Run it
```bash
python linda_scraper.py
```

### 5. Automate locally (Windows Task Scheduler)

Create `run_linda.bat`:
```bat
@echo off
cd /d "C:\path\to\your\project\scraper"
python linda_scraper.py
```
Then in **Task Scheduler** → Create Basic Task → Weekly → point to the `.bat` file.

> ⚠️ Your PC must be ON at the scheduled time.

---

## Tuning

| Setting | Default | Description |
|---|---|---|
| `NUM_WORKERS` | `4` | Parallel Chrome windows — increase for speed, decrease if PC is slow |
| `WAIT_PAGE` | `15` | Seconds to wait for listing page to load |
| `WAIT_AD` | `15` | Seconds to wait for ad detail page |
| `MAX_RETRIES` | `3` | Retries per ad before giving up |

Each Chrome window uses ~400 MB RAM. With 4 workers you need ~2 GB free.

---

## Output CSV Columns

| Column | Example | Description |
|---|---|---|
| `ad_url` | `https://lindacars.com/vehicle/...` | Direct link to the ad |
| `Make` | `Jetour` | Car brand |
| `Model` | `Jetour T2` | Full model name |
| `Price` | `89000` | Listed price |
| `Year` | `2026` | Model year |
| `Mileage` | `13,000 km` | Odometer reading |
| `Fuel` | `Petrol` | Fuel type (split from Engine field) |
| `Engine` | `2.0 L (254 hp)` | Engine size and power |
| `Transmission` | `Dual clutch (DCT/DSG), 7-speed` | Gearbox type |
| `Drive` | `4WD` | Drive type |
| `Trim` | `Traveler+` | Trim level |
| `BodyType` | `SUV 5-doors` | Body style |
| `Color` | `Black` | Exterior color |
| `Seats` | `5` | Number of seats |
| `Condition` | `Used` | Used or New |
| `Owners` | `1 owner` | Previous owners |
| `Accidents` | `No accidents` | Accident history |
| `GeneralCondition` | `Perfect condition` | Overall rating |
| `BodyCondition` | `Perfect` | Body rating |
| `MechanicalCondition` | `Perfect` | Mechanical rating |
| `InteriorCondition` | `Perfect` | Interior rating |
| `Specs` | `GCC specs` | Regional specification |
| `EmissionStandard` | `Euro 6d` | Emission standard |
| `Images` | `https://content...` | Comma-separated full-size image URLs |
| `Status` | `NEW` | NEW / UPDATED / UNCHANGED / REMOVED |
| `Change_Details` | `Price: 89000 -> 85000` | What changed (UPDATED rows only) |
| `Prev_Price` | `89000` | Old price (UPDATED rows only) |
| `Prev_Mileage` | `13,000 km` | Old mileage (UPDATED rows only) |
| `Scraped_Date` | `2025-06-15` | Date this row was scraped |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Chrome won't open | Make sure Google Chrome is installed and up to date |
| All ads fail to scrape | Increase `WAIT_AD` to `20` in the config — site may be slow |
| Color / Fuel / Engine empty | The site may have updated its HTML — check the selectors |
| Email not sending | Verify the App Password and that 2-Step Verification is ON |
| GitHub Actions fails | Go to Actions tab → click the red run → expand the failed step to read the error |
| Workflow file not found | Make sure it was uploaded to `.github/workflows/` not just `workflows/` |
| `Linda Cars ad/` folder missing | Created automatically on first run — just run once |
