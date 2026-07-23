"""
pipeline.py — runs locally on your ThinkPad (not in Colab).
Combines the functions you built in Colab with SQLite storage.
Run manually with: python pipeline.py
Or on a schedule via cron (see instructions at the bottom of this file).

NOTE: This version changes the filings table schema (new columns added).
If you have an existing etf_data.db from before, delete it first:
    rm etf_data.db
so it can be rebuilt with the new columns.
"""

import os
import re
import zipfile
import sqlite3
import requests
import pandas as pd

# ---- EDIT THIS with your real name and email — SEC blocks generic User-Agents ----
HEADERS = {"User-Agent": "YourName your-email@example.com"}

FORM_TYPES = ["S-1", "N-1A", "485BPOS"]

# ---- Category (bucket) rules ----
CATEGORY_RULES = {
    "digital": ["bitcoin", "ether", "ethereum", "digital asset", "blockchain", "crypto"],
    "fixed_income": ["treasury", "bond", "credit", "income", "duration"],
    "derivatives": ["options", "futures", "leveraged", "inverse", "derivative", "covered call"],
    "thematic": ["ai", "robotics", "cannabis", "innovation", "clean energy", "cybersecurity", "space"],
    "equity": ["equity", "growth", "value", "large cap", "small cap"],
}

# ---- Fund type rules (ETF vs Mutual Fund) ----
ETF_KEYWORDS = ["etf", "exchange traded", "exchange-traded"]

# ---- Management style rules (only meaningful for ETFs) ----
PASSIVE_KEYWORDS = ["index", "s&p", "nasdaq", "msci", "russell", "ftse", "passive", "tracking"]
ACTIVE_KEYWORDS = ["active", "actively managed"]

# ---- Industry rules (only meaningful for thematic bucket) ----
INDUSTRY_RULES = {
    "ai": "Artificial Intelligence",
    "robotics": "Robotics & Automation",
    "cannabis": "Cannabis",
    "innovation": "Innovation & Disruptive Tech",
    "clean energy": "Clean Energy",
    "cybersecurity": "Cybersecurity",
    "space": "Space & Aerospace",
}

DB_PATH = "etf_data.db"

# ---- EDIT THIS with the real N-PORT zip URL from the SEC page for the quarter you want ----
# https://www.sec.gov/dera/data/form-n-port-data-sets
NPORT_ZIP_URL = "https://www.sec.gov/files/dera/data/form-n-port-data-sets/2026q1_nport.zip"
NPORT_DIR = "nport_data"

# ---- EDIT THESE if the column names differ after you inspect the actual TSVs ----
COL_FUND_NAME = "SERIES_NAME"
COL_TOTAL_ASSETS = "TOTAL_ASSETS"
COL_ACCESSION = "ACCESSION_NUMBER"
COL_PERIOD = "REPORT_ENDING_PERIOD"


def edgar_search(form_type, start_date, end_date):
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": '"' + form_type + '"',
        "forms": form_type,
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
    }
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def categorize(fund_name):
    """Returns a bucket string based on keyword matching."""
    name = str(fund_name).lower()
    for bucket, keywords in CATEGORY_RULES.items():
        if any(kw in name for kw in keywords):
            return bucket
    return "other"


def get_fund_type(fund_name):
    """Returns 'ETF' or 'Mutual Fund' based on keyword matching."""
    name = str(fund_name).lower()
    if any(kw in name for kw in ETF_KEYWORDS):
        return "ETF"
    return "Mutual Fund"


def get_management_style(fund_name, fund_type):
    """Returns 'Active', 'Passive', or 'Unknown' — only meaningful for ETFs."""
    if fund_type != "ETF":
        return None
    name = str(fund_name).lower()
    if any(kw in name for kw in PASSIVE_KEYWORDS):
        return "Passive"
    if any(kw in name for kw in ACTIVE_KEYWORDS):
        return "Active"
    return "Unknown"


def get_industry(fund_name):
    """Returns an industry label — only meaningful for the 'thematic' bucket."""
    name = str(fund_name).lower()
    for keyword, label in INDUSTRY_RULES.items():
        if keyword in name:
            return label
    return "Other Thematic"


def get_filing_description(cik, filing_id):
    """
    Best-effort fetch of a short description from the actual filing document.
    filing_id is the raw EDGAR hit id, formatted 'accession-number:filename'.
    Returns None if it can't be fetched or parsed (this is best-effort, not guaranteed).
    """
    try:
        if not cik or not filing_id or ":" not in filing_id:
            return None
        accession, filename = filing_id.split(":", 1)
        accession_nodash = accession.replace("-", "")
        cik_nolead = str(int(cik))
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_nolead}/{accession_nodash}/{filename}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        text = r.text
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        match = re.search(r"investment objective.{0,400}", clean, re.IGNORECASE)
        if match:
            return match.group(0)[:400]
        return clean[:400] if clean else None
    except Exception:
        return None


def get_new_filings(start_date, end_date):
    rows = []
    for form in FORM_TYPES:
        data = edgar_search(form, start_date, end_date)
        for hit in data.get("hits", {}).get("hits", []):
            src = hit["_source"]
            fund_name = ", ".join(src.get("display_names", []))
            ciks = src.get("ciks", [])
            cik0 = ciks[0] if ciks else None

            fund_type = get_fund_type(fund_name)
            management_style = get_management_style(fund_name, fund_type)
            bucket = categorize(fund_name)
            industry = get_industry(fund_name) if bucket == "thematic" else None
            description = None
            if bucket == "thematic":
                description = get_filing_description(cik0, hit.get("_id"))

            rows.append({
                "fund_name": fund_name,
                "form_type": src.get("root_forms", [form])[0] if src.get("root_forms") else form,
                "filing_date": src.get("file_date"),
                "filer_cik": ", ".join(ciks) if ciks else None,
                "fund_type": fund_type,
                "management_style": management_style,
                "bucket": bucket,
                "industry": industry,
                "description": description,
            })
    df = pd.DataFrame(rows).drop_duplicates()
    return df


def download_and_extract_nport():
    """Downloads the N-PORT zip (if not already present) and extracts it."""
    zip_path = "nport.zip"
    if not os.path.exists(zip_path):
        r = requests.get(NPORT_ZIP_URL, headers=HEADERS, stream=True)
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded {zip_path}")

    if not os.path.exists(NPORT_DIR):
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(NPORT_DIR)
        print(f"Extracted into {NPORT_DIR}/")


def get_aum_flows():
    """
    Parses the downloaded N-PORT TSVs, computes AUM and month-over-month change,
    and returns a DataFrame ready to write into the aum_flows table.
    Returns an empty DataFrame if the files aren't present or parsing fails.
    """
    fund_info_path = os.path.join(NPORT_DIR, "FUND_REPORTED_INFO.tsv")
    submissions_path = os.path.join(NPORT_DIR, "SUBMISSION.tsv")

    if not (os.path.exists(fund_info_path) and os.path.exists(submissions_path)):
        print(f"N-PORT files not found in {NPORT_DIR}/ — skipping AUM ingestion.")
        return pd.DataFrame()

    try:
        fund_info = pd.read_csv(fund_info_path, sep="\t", low_memory=False)
        submissions = pd.read_csv(submissions_path, sep="\t", low_memory=False)

        merged = fund_info.merge(
            submissions[[COL_ACCESSION, COL_PERIOD]], on=COL_ACCESSION, how="left"
        )
        merged = merged[[COL_FUND_NAME, COL_TOTAL_ASSETS, COL_PERIOD]].dropna()
        merged.columns = ["fund_name", "total_net_assets", "period"]
        merged["period"] = pd.to_datetime(merged["period"])
        merged = merged.sort_values(["fund_name", "period"])

        merged["aum_change"] = merged.groupby("fund_name")["total_net_assets"].diff()
        merged["bucket"] = merged["fund_name"].apply(categorize)

        return merged
    except Exception as e:
        print(f"Failed to parse N-PORT data: {e}")
        return pd.DataFrame()


def setup_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS filings
        (fund_name TEXT, form_type TEXT, filing_date TEXT, filer_cik TEXT,
         fund_type TEXT, management_style TEXT, bucket TEXT, industry TEXT, description TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS aum_flows
        (fund_name TEXT, total_net_assets REAL, period TEXT, aum_change REAL, bucket TEXT)""")
    conn.commit()


def run_all(start_date, end_date):
    """Pulls fresh filings for the given date range and writes them into SQLite.
    Also attempts to ingest N-PORT AUM data (quarterly; safe to run repeatedly —
    it skips the download if the zip is already present)."""
    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)

    filings_df = get_new_filings(start_date, end_date)
    if not filings_df.empty:
        filings_df.to_sql("filings", conn, if_exists="append", index=False)
        print(f"Wrote {len(filings_df)} filings to {DB_PATH}")
    else:
        print("No new filings found for this range.")

    try:
        download_and_extract_nport()
        aum_df = get_aum_flows()
        if not aum_df.empty:
            aum_df.to_sql("aum_flows", conn, if_exists="replace", index=False)
            print(f"Wrote {len(aum_df)} AUM rows to {DB_PATH}")
        else:
            print("No AUM data available to write.")
    except Exception as e:
        print(f"AUM ingestion failed (filings still updated fine): {e}")

    conn.close()


if __name__ == "__main__":
    import datetime
    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)
    run_all(week_ago.isoformat(), today.isoformat())

# ---- To schedule this daily with cron ----
# 1. Run: crontab -e
# 2. Add this line (adjust paths to match your actual project folder):
#    0 6 * * * /path/to/etf-dashboard/venv/bin/python /path/to/etf-dashboard/pipeline.py
# This runs the pipeline every day at 6am and appends new filings to etf_data.db.
