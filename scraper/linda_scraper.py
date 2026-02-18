import os
import re
import time
import datetime
import threading
import smtplib
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from queue import Queue, Empty
from urllib.parse import urljoin

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, TimeoutException

# ================= CONFIG =================
BASE_URL    = "https://www.lindacars.com"
START_URL   = "https://www.lindacars.com/buy-car?hotDeals=false&page-size=12&sort-by=id&sort-order=desc&lang=en&page="
SAVE_DIR    = "Linda Cars ad"     # folder where CSVs are saved
WAIT_PAGE   = 15                  # seconds to wait for listing page tiles
WAIT_AD     = 15                  # seconds to wait for ad detail page
MAX_RETRIES = 3                   # retries per ad
RETRY_WAIT  = 3                   # seconds between retries
NUM_WORKERS = 4                   # parallel Chrome windows

# Chrome profile — each worker gets its own subfolder (local PC only)
# Replace "User" with your actual Windows username
CHROME_PROFILE = r"C:\Users\User\AppData\Local\Google\Chrome\User Data\Selenium_Linda"

# ── EMAIL CONFIG ──────────────────────────────────────────────────────────────
# On GitHub Actions these are read from repository Secrets automatically.
# For local use, replace the fallback strings with your real values.
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER",   "your_gmail@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "xxxx xxxx xxxx xxxx")
EMAIL_TO_RAW   = os.environ.get("EMAIL_TO",       "your_email@gmail.com")
EMAIL_TO       = [e.strip() for e in EMAIL_TO_RAW.split(",") if e.strip()]
EMAIL_ENABLED  = "your_gmail" not in EMAIL_SENDER  # auto-disabled if not configured
# ─────────────────────────────────────────────────────────────────────────────

DATE_COL   = "Scraped_Date"
print_lock = threading.Lock()

# True when running on GitHub Actions (headless mode, no Chrome profile needed)
IS_CI = os.environ.get("CI", "false").lower() == "true"


def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


# =====================================================================
# IMAGE URL TRANSFORM
# fit-288xauto  ->  fit-1324xauto  (full-size version on this platform)
# =====================================================================
def transform_image_url(url: str) -> str:
    return re.sub(r'fit-\d+xauto', 'fit-1324xauto', url)


# =====================================================================
# CHROME DRIVER INIT
# =====================================================================
def init_driver(worker_id: int = 0) -> webdriver.Chrome:
    opts = Options()

    if IS_CI:
        # GitHub Actions: headless, no profile
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
    else:
        # Local: separate Chrome profile per worker to avoid conflicts
        profile_dir = f"{CHROME_PROFILE}_worker{worker_id}"
        opts.add_argument(f"user-data-dir={profile_dir}")
        opts.add_argument("--start-maximized")
        # opts.add_argument("--headless=new")  # uncomment to hide windows locally

    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    return webdriver.Chrome(options=opts)


# =====================================================================
# PAGE WAITS
# =====================================================================
def wait_for_ads_on_page(driver):
    try:
        WebDriverWait(driver, WAIT_PAGE).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a.dd-product-tile"))
        )
    except TimeoutException:
        safe_print("  [!] Timed out waiting for listing page.")


def wait_for_ad_detail(driver) -> bool:
    """Poll several possible content selectors. Returns True when page is ready."""
    CANDIDATES = [
        "span.p-brand",
        "span.p-name",
        ".MuiCardContent-root",
        "data",
    ]
    deadline = time.time() + WAIT_AD
    while time.time() < deadline:
        for sel in CANDIDATES:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el:
                    time.sleep(0.5)  # let React finish rendering
                    return True
            except Exception:
                pass
        time.sleep(0.4)
    return False


# =====================================================================
# URL COLLECTION — auto-stops when a page returns 0 new ads
# =====================================================================
def collect_ad_urls(driver) -> list:
    ads, seen = [], set()
    page = 0

    while True:
        url = f"{START_URL}{page}"
        safe_print(f"  Collecting page {page}: {url}")
        driver.get(url)
        wait_for_ads_on_page(driver)

        links = driver.find_elements(By.CSS_SELECTOR, "a.dd-product-tile")
        new_count = 0
        for link in links:
            href = link.get_attribute("href")
            if href:
                full = urljoin(BASE_URL, href.split("?")[0])
                if full not in seen:
                    seen.add(full)
                    ads.append(full)
                    new_count += 1

        safe_print(f"    -> {new_count} new ads on page {page} (total: {len(ads)})")

        if new_count == 0:
            safe_print(f"  No new ads on page {page} — stopping collection.")
            break

        page += 1

    return ads


# =====================================================================
# SPEC READING
# Confirmed HTML structure (deal-drive platform):
#
#   div.mui-1d58shw                          <- one spec row
#     span.MuiTypography-body2               <- LEFT: label container
#       div > div > svg + span("Year")       <- label text in last <span>
#     span.MuiTypography-body1               <- RIGHT: value
#       "2026"
#
# Special rows:
#   "model"      -> value has <span.p-brand>Make</span><span> ModelSuffix</span>
#   "body color" -> value has nested span.MuiTypography-body2 with color name
#   "body type"  -> value has two div.MuiBox-root ("SUV" + "5-doors")
#   "engine"     -> value is "Petrol 2.0 L (254 hp)" -> split into Fuel + Engine
# =====================================================================

YEAR_RE    = re.compile(r'^(19[5-9]\d|20[0-3]\d)$')
MILEAGE_RE = re.compile(r'km', re.IGNORECASE)

# Maps label text (lowercase) from the left side -> CSV column name
LABEL_MAP = {
    # car identity
    "trim"                : "Trim",
    "body type"           : "BodyType",
    "body color"          : "Color",
    "colour"              : "Color",
    "color"               : "Color",
    # mechanical
    "transmission"        : "Transmission",
    "gearbox"             : "Transmission",
    "drive"               : "Drive",
    "fuel type"           : "Fuel",
    "fuel"                : "Fuel",
    # year / usage
    "year"                : "Year",
    "mileage"             : "Mileage",
    "condition"           : "Condition",
    "seat count"          : "Seats",
    # history
    "previous owners"     : "Owners",
    "accidents"           : "Accidents",
    "general condition"   : "GeneralCondition",
    "body condition"      : "BodyCondition",
    "mechanical condition": "MechanicalCondition",
    "interior condition"  : "InteriorCondition",
    # specs
    "regional specs"      : "Specs",
    "emission standard"   : "EmissionStandard",
    "emission co2"        : "EmissionCO2",
}

# Splits "Petrol 2.0 L (254 hp)" -> ("Petrol", "2.0 L (254 hp)")
ENGINE_FUEL_RE = re.compile(r'^([A-Za-z][\w\s\-]*?)\s+(\d.*)', re.DOTALL)


def safe_get(driver, selector) -> str:
    try:
        return driver.find_element(By.CSS_SELECTOR, selector).text.strip()
    except NoSuchElementException:
        return ""


def extract_label(row) -> str:
    """Read the LEFT (label) text from a spec row."""
    try:
        label_el    = row.find_element(By.CSS_SELECTOR, "span.MuiTypography-body2")
        label_spans = label_el.find_elements(By.CSS_SELECTOR, "span")
        if label_spans:
            # Last <span> is the text label (earlier ones may be SVG wrappers)
            return label_spans[-1].text.strip().lower()
        return label_el.text.strip().lower()
    except Exception:
        return ""


def extract_value(row, label: str) -> str:
    """
    Read the RIGHT (value) from a spec row.
    Handles the three special-case layouts confirmed from the HTML.
    """
    try:
        value_el = row.find_element(By.CSS_SELECTOR, "span.MuiTypography-body1")

        # Body color: text is in a nested span.MuiTypography-body2 (color name beside swatch)
        if label == "body color":
            try:
                color_span = value_el.find_element(
                    By.CSS_SELECTOR, "span.MuiTypography-body2"
                )
                return color_span.text.strip()
            except Exception:
                pass

        # Body type: two MuiBox-root divs e.g. "SUV" + "5-doors" -> "SUV 5-doors"
        if label == "body type":
            boxes = value_el.find_elements(By.CSS_SELECTOR, "div.MuiBox-root")
            if boxes:
                return " ".join(b.text.strip() for b in boxes if b.text.strip())

        return value_el.text.strip()
    except Exception:
        return ""


def split_fuel_engine(raw: str):
    """
    'Petrol 2.0 L (254 hp)'  ->  fuel='Petrol', engine='2.0 L (254 hp)'
    'Petrol Plug-in Hybrid 2.4 L' -> fuel='Petrol Plug-in Hybrid', engine='2.4 L'
    Returns ('', raw) if pattern doesn't match.
    """
    m = ENGINE_FUEL_RE.match(raw.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", raw.strip()


def read_all_specs(driver) -> dict:
    """
    Walk every spec row and read label (left) + value (right).
    Returns a dict with CSV column names as keys.
    Also stores MakeFromRow and ModelSuffix for the combined Model row.
    """
    specs = {}

    try:
        rows = driver.find_elements(By.CSS_SELECTOR, ".MuiCardContent-root .mui-1d58shw")

        for row in rows:
            try:
                label = extract_label(row)
                if not label:
                    continue

                # ── "Model" row: value = "<p-brand>Jetour</p-brand><span> T2</span>" ──
                # Pull Make from p-brand span, Model suffix from remaining text
                if label == "model":
                    try:
                        value_el  = row.find_element(By.CSS_SELECTOR, "span.MuiTypography-body1")
                        make_span = value_el.find_element(By.CSS_SELECTOR, "span.p-brand")
                        make_text = make_span.text.strip()
                        full_text  = value_el.text.strip()
                        model_text = full_text.replace(make_text, "").strip()
                        if make_text and "MakeFromRow" not in specs:
                            specs["MakeFromRow"] = make_text
                        if model_text and "ModelSuffix" not in specs:
                            specs["ModelSuffix"] = model_text
                    except Exception:
                        pass
                    continue

                value = extract_value(row, label)
                if not value:
                    continue

                # ── "Engine" row: "Petrol 2.0 L (254 hp)" -> Fuel + Engine ──
                if label == "engine":
                    fuel, engine_size = split_fuel_engine(value)
                    if fuel and "Fuel" not in specs:
                        specs["Fuel"] = fuel
                    if engine_size and "Engine" not in specs:
                        specs["Engine"] = engine_size
                    continue

                col = LABEL_MAP.get(label)
                if col and col not in specs:
                    specs[col] = value

            except Exception:
                continue

    except Exception:
        pass

    # ── Fallback: pattern scan for Year & Mileage if label-based read missed them ──
    if "Year" not in specs or "Mileage" not in specs:
        try:
            all_body1 = driver.find_elements(
                By.CSS_SELECTOR, ".MuiCardContent-root span.MuiTypography-body1"
            )
            for span in all_body1:
                try:
                    text = span.text.strip()
                    if not text:
                        continue
                    if "Year" not in specs and YEAR_RE.match(text):
                        specs["Year"] = text
                    elif "Mileage" not in specs and MILEAGE_RE.search(text):
                        specs["Mileage"] = text
                except Exception:
                    continue
        except Exception:
            pass

    return specs


def read_images(driver) -> str:
    """Collect all deal-drive images, upgrade to fit-1324xauto, deduplicate."""
    images, seen_hashes = [], set()
    try:
        img_elements = driver.find_elements(By.CSS_SELECTOR, ".MuiStack-root img")
        for img in img_elements:
            try:
                src = img.get_attribute("src") or img.get_attribute("data-src") or ""
                if not src or "content.deal-drive.com" not in src:
                    continue
                full_url = transform_image_url(src)
                m   = re.search(r'/thumbs/([a-f0-9]+)/', full_url)
                key = m.group(1) if m else full_url
                if key not in seen_hashes:
                    seen_hashes.add(key)
                    images.append(full_url)
            except Exception:
                continue
    except Exception:
        pass
    return ",".join(images)


# =====================================================================
# SCRAPE SINGLE AD  (with retry)
# =====================================================================
def scrape_ad_details(driver, ad_url: str) -> dict:
    data = {"ad_url": ad_url}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            driver.get(ad_url)
            if not wait_for_ad_detail(driver):
                raise TimeoutException(f"Page not ready after {WAIT_AD}s")

            # Read all specs first — the "model" row gives us Make + Model
            specs  = read_all_specs(driver)
            images = read_images(driver)

            # Make: p-brand span on page (most reliable), fallback to spec row
            make = safe_get(driver, "span.p-brand") or specs.get("MakeFromRow", "")

            # Model: p-name span if present, otherwise combine MakeFromRow + ModelSuffix
            model_suffix = specs.get("ModelSuffix", "")
            p_name       = safe_get(driver, "span.p-name")
            if p_name:
                model = p_name
            elif make and model_suffix:
                model = f"{make} {model_suffix}".strip()
            else:
                model = model_suffix or make

            # Price from <data> tag
            price = ""
            try:
                el    = driver.find_element(By.CSS_SELECTOR, "data")
                price = el.get_attribute("value") or el.text.strip()
            except NoSuchElementException:
                pass

            if not make and not model and not price:
                raise ValueError("All core fields empty — page likely not loaded yet")

            data.update({
                "Make"               : make,
                "Model"              : model,
                "Description"        : model,
                "Price"              : price,
                "Color"              : specs.get("Color", ""),
                "Year"               : specs.get("Year", ""),
                "Mileage"            : specs.get("Mileage", ""),
                "Fuel"               : specs.get("Fuel", ""),
                "Engine"             : specs.get("Engine", ""),
                "Transmission"       : specs.get("Transmission", ""),
                "Drive"              : specs.get("Drive", ""),
                "Trim"               : specs.get("Trim", ""),
                "BodyType"           : specs.get("BodyType", ""),
                "Seats"              : specs.get("Seats", ""),
                "Condition"          : specs.get("Condition", ""),
                "Owners"             : specs.get("Owners", ""),
                "Accidents"          : specs.get("Accidents", ""),
                "GeneralCondition"   : specs.get("GeneralCondition", ""),
                "BodyCondition"      : specs.get("BodyCondition", ""),
                "MechanicalCondition": specs.get("MechanicalCondition", ""),
                "InteriorCondition"  : specs.get("InteriorCondition", ""),
                "Specs"              : specs.get("Specs", ""),
                "EmissionStandard"   : specs.get("EmissionStandard", ""),
                "Images"             : images,
            })

            safe_print(
                f"    [OK] {make} {model} | {price} | "
                f"Y:{data['Year']} | KM:{data['Mileage']} | "
                f"Fuel:{data['Fuel']} | Eng:{data['Engine']} | "
                f"Trans:{data['Transmission']} | Trim:{data['Trim']} | "
                f"Color:{data['Color']}"
            )
            return data

        except Exception as e:
            safe_print(f"  [!] Attempt {attempt}/{MAX_RETRIES} — {ad_url} -> {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT)

    # All retries failed — save URL with empty fields so nothing is lost
    safe_print(f"  [✗] FAILED after {MAX_RETRIES} attempts: {ad_url}")
    data.update({
        "Make": "", "Model": "", "Description": "", "Price": "",
        "Color": "", "Year": "", "Mileage": "", "Fuel": "",
        "Engine": "", "Transmission": "", "Drive": "", "Trim": "",
        "BodyType": "", "Seats": "", "Condition": "", "Owners": "",
        "Accidents": "", "GeneralCondition": "", "BodyCondition": "",
        "MechanicalCondition": "", "InteriorCondition": "",
        "Specs": "", "EmissionStandard": "", "Images": "",
    })
    return data


# =====================================================================
# WORKER THREAD
# =====================================================================
def worker_scrape(worker_id: int, url_queue: Queue, results: list, counter: list, total: int):
    driver = init_driver(worker_id)
    safe_print(f"  [Worker {worker_id}] Browser ready.")
    try:
        while True:
            try:
                ad_url = url_queue.get_nowait()
            except Empty:
                break

            with print_lock:
                counter[0] += 1
                idx = counter[0]

            safe_print(f"  [W{worker_id}] [{idx}/{total}] {ad_url}")
            result = scrape_ad_details(driver, ad_url)
            results.append(result)
            url_queue.task_done()
    finally:
        driver.quit()
        safe_print(f"  [Worker {worker_id}] Done & closed.")


# =====================================================================
# RECONCILER
# =====================================================================
def get_csv_path(today: str) -> str:
    os.makedirs(SAVE_DIR, exist_ok=True)
    return os.path.join(SAVE_DIR, f"{today}.csv")


def find_latest_csv(exclude_date: str = ""):
    if not os.path.isdir(SAVE_DIR):
        return None
    csvs = []
    for f in os.listdir(SAVE_DIR):
        if f.endswith(".csv"):
            name = f[:-4]
            if name != exclude_date:
                csvs.append((name, os.path.join(SAVE_DIR, f)))
    if not csvs:
        return None
    csvs.sort(key=lambda x: x[0], reverse=True)
    return csvs[0][1]


def reconcile(new_df: pd.DataFrame, today: str) -> pd.DataFrame:
    new_df           = new_df.copy()
    new_df[DATE_COL] = today

    prev_path = find_latest_csv(exclude_date=today)
    if prev_path is None:
        print(f"  No previous data — marking all {len(new_df)} ads as NEW.")
        new_df["Status"]         = "NEW"
        new_df["Change_Details"] = ""
        new_df["Prev_Price"]     = ""
        new_df["Prev_Mileage"]   = ""
        return new_df

    try:
        prev_df = pd.read_csv(prev_path, dtype=str).fillna("")
    except Exception as e:
        print(f"  Could not read previous file ({e}) — marking all as NEW.")
        new_df["Status"]         = "NEW"
        new_df["Change_Details"] = ""
        new_df["Prev_Price"]     = ""
        new_df["Prev_Mileage"]   = ""
        return new_df

    print(f"  Comparing against: {prev_path}  ({len(prev_df)} rows)")

    # Only non-REMOVED rows from last run are the active baseline
    active_prev = prev_df[prev_df["Status"].str.upper() != "REMOVED"].set_index("ad_url")

    new_df.set_index("ad_url", inplace=True)
    new_df["Status"]         = "NEW"
    new_df["Change_Details"] = ""
    new_df["Prev_Price"]     = ""
    new_df["Prev_Mileage"]   = ""

    for url in new_df.index:
        if url in active_prev.index:
            old_price   = active_prev.at[url, "Price"]   if "Price"   in active_prev.columns else ""
            old_mileage = active_prev.at[url, "Mileage"] if "Mileage" in active_prev.columns else ""
            new_price   = str(new_df.at[url, "Price"])
            new_mileage = str(new_df.at[url, "Mileage"])

            price_changed   = old_price   != new_price
            mileage_changed = old_mileage != new_mileage

            if not price_changed and not mileage_changed:
                new_df.at[url, "Status"] = "UNCHANGED"
            else:
                new_df.at[url, "Status"] = "UPDATED"
                changes = []
                if price_changed:
                    changes.append(f"Price: {old_price} -> {new_price}")
                if mileage_changed:
                    changes.append(f"Mileage: {old_mileage} -> {new_mileage}")
                new_df.at[url, "Change_Details"] = " | ".join(changes)
                new_df.at[url, "Prev_Price"]     = old_price
                new_df.at[url, "Prev_Mileage"]   = old_mileage

    # Ads that vanished since last run -> REMOVED
    removed_urls = active_prev.index.difference(new_df.index)
    if len(removed_urls):
        removed                   = active_prev.loc[removed_urls].copy()
        removed["Status"]         = "REMOVED"
        removed["Change_Details"] = ""
        removed["Prev_Price"]     = ""
        removed["Prev_Mileage"]   = ""
        if DATE_COL not in removed.columns:
            removed[DATE_COL] = ""
        new_df = pd.concat([new_df, removed])

    new_df.reset_index(inplace=True)
    return new_df


def print_reconcile_summary(df: pd.DataFrame):
    if "Status" not in df.columns:
        return
    counts = df["Status"].value_counts()
    print("\n  ┌─────────────────────────┐")
    print(  "  │   Reconciliation Report │")
    print(  "  ├──────────────┬──────────┤")
    for status in ["NEW", "UPDATED", "UNCHANGED", "REMOVED"]:
        n = counts.get(status, 0)
        print(f"  │  {status:<12}  │  {n:>6}  │")
    print(  "  └──────────────┴──────────┘")


# =====================================================================
# EMAIL
# =====================================================================
def send_email(csv_path: str, summary: dict, today: str):
    if not EMAIL_ENABLED:
        print("  Email disabled (EMAIL_SENDER not configured) — skipping.")
        return

    try:
        msg            = MIMEMultipart()
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = ", ".join(EMAIL_TO)
        msg["Subject"] = f"Linda Cars — Weekly Scrape {today}"

        body_lines = [
            f"Linda Cars scrape completed on {today}.",
            "",
            "Summary:",
            f"  NEW        : {summary.get('NEW', 0)}",
            f"  UPDATED    : {summary.get('UPDATED', 0)}",
            f"  UNCHANGED  : {summary.get('UNCHANGED', 0)}",
            f"  REMOVED    : {summary.get('REMOVED', 0)}",
            f"  TOTAL ROWS : {summary.get('TOTAL', 0)}",
            "",
            "The full CSV is attached.",
            "",
            "— Linda Cars Scraper (automated)",
        ]
        msg.attach(MIMEText("\n".join(body_lines), "plain"))

        with open(csv_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(csv_path)}"'
        )
        msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())

        print(f"  Email sent to: {', '.join(EMAIL_TO)}")

    except Exception as e:
        print(f"  [!] Email failed: {e}")
        traceback.print_exc()


# =====================================================================
# MAIN
# =====================================================================
def main():
    today    = datetime.date.today().isoformat()
    csv_path = get_csv_path(today)

    # Phase 1: collect all ad URLs (stops automatically when no new ads found)
    print("=== PHASE 1: Collecting ad URLs ===")
    collector = init_driver(worker_id=99)
    try:
        ad_urls = collect_ad_urls(collector)
    finally:
        collector.quit()

    total = len(ad_urls)
    print(f"\n  Total ads found: {total}")

    if total == 0:
        print("  No ads found — aborting.")
        return

    # Phase 2: parallel scrape with NUM_WORKERS Chrome windows
    print(f"\n=== PHASE 2: Scraping {total} ads with {NUM_WORKERS} parallel windows ===")

    url_queue = Queue()
    for url in ad_urls:
        url_queue.put(url)

    results = []
    counter = [0]
    threads = []

    for wid in range(NUM_WORKERS):
        t = threading.Thread(
            target=worker_scrape,
            args=(wid, url_queue, results, counter, total),
            daemon=True,
            name=f"Worker-{wid}",
        )
        t.start()
        threads.append(t)
        time.sleep(2)  # stagger starts to avoid Chrome race condition

    for t in threads:
        t.join()

    # Phase 3: reconcile with previous run and save CSV
    print("\n=== PHASE 3: Reconciling & Saving ===")
    raw_df   = pd.DataFrame(results)
    final_df = reconcile(raw_df, today)

    os.makedirs(SAVE_DIR, exist_ok=True)
    final_df.to_csv(csv_path, index=False)

    print_reconcile_summary(final_df)
    print(f"\n  Saved -> {csv_path}  ({len(final_df)} total rows)")

    # Phase 4: email the CSV
    print("\n=== PHASE 4: Sending Email ===")
    counts = final_df["Status"].value_counts().to_dict() if "Status" in final_df.columns else {}
    counts["TOTAL"] = len(final_df)
    send_email(csv_path, counts, today)

    print("\nDone.")


if __name__ == "__main__":
    main()
