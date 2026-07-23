import os
import re
import time
import zipfile
import sqlite3
import requests
import pandas as pd
 
# ---- SEC requires a real contact email in the User-Agent, or it blocks requests. ----
# In GitHub Actions this comes from the SEC_EMAIL secret (never committed to the repo).
# For local runs, either `export SEC_EMAIL="Your Name you@example.com"` in your terminal
# first, or just replace the fallback string below with your own name and email.
SEC_EMAIL = os.environ.get("SEC_EMAIL", "YourName your-email@example.com")
HEADERS = {"User-Agent": SEC_EMAIL}
 
FORM_TYPES = ["S-1", "N-1A", "485BPOS"]
 
# ---- Category (bucket) rules ----
# Terms are matched on WORD BOUNDARIES (see _has_term), so "ai" no longer matches
# inside "sustAInable" and "space" no longer matches inside "aeroSPACE".
CATEGORY_RULES = {
    "digital": ["bitcoin", "ether", "ethereum", "digital asset", "blockchain", "crypto"],
    "fixed_income": ["treasury", "bond", "credit", "income", "duration", "municipal", "high yield"],
    "derivatives": ["options", "futures", "leveraged", "inverse", "derivative", "covered call", "buffer"],
    "thematic": ["ai", "artificial intelligence", "machine learning", "robotics", "cannabis",
                 "innovation", "clean energy", "cybersecurity", "space", "semiconductor", "genomic"],
    "equity": ["equity", "growth", "value", "large cap", "small cap", "mid cap", "dividend"],
}
 
# ---- Fund type rules (ETF vs Mutual Fund) ----
# Name-based signals. Brand names are a heuristic assist for the many ETFs whose
# series name omits "ETF" — verify against the filing for anything that matters.
ETF_KEYWORDS = ["etf", "exchange traded", "exchange-traded"]
ETF_BRANDS = ["ishares", "spdr", "invesco qqq", "proshares", "direxion", "global x",
              "ark ", "vaneck", "wisdomtree", "xtrackers", "franklin ftse"]
 
# ---- Management style rules (only meaningful for ETFs) ----
PASSIVE_KEYWORDS = ["index", "s&p", "nasdaq", "msci", "russell", "ftse", "passive", "tracking", "bloomberg"]
ACTIVE_KEYWORDS = ["active", "actively managed"]
 
# ---- Industry rules (only meaningful for thematic bucket) ----
# label -> list of terms that map to it (first label with a match wins).
INDUSTRY_RULES = {
    "Artificial Intelligence": ["ai", "artificial intelligence", "machine learning"],
    "Robotics & Automation": ["robotics", "automation"],
    "Cannabis": ["cannabis", "marijuana"],
    "Innovation & Disruptive Tech": ["innovation", "disruptive"],
    "Clean Energy": ["clean energy", "solar", "renewable"],
    "Cybersecurity": ["cybersecurity", "cyber security"],
    "Space & Aerospace": ["space", "aerospace"],
    "Semiconductors": ["semiconductor", "chip"],
}
 
DB_PATH = "etf_data.db"
 
# ---- SEC request tuning ----
SEC_MIN_INTERVAL = 0.15      # seconds between SEC requests (~6-7/sec, under the 10/sec limit)
SEC_MAX_RETRIES = 4          # retries on 429 / 403 / 5xx / network errors
EFTS_PAGE_SIZE = 100         # hits per full-text-search page
EFTS_RESULT_CAP = 10000      # SEC caps full-text search at 10,000 total results per query
 
# ---- EDIT THIS with the real N-PORT zip URL from the SEC page for the quarter you want ----
# https://www.sec.gov/dera/data/form-n-port-data-sets
NPORT_ZIP_URL = "https://www.sec.gov/files/dera/data/form-n-port-data-sets/2026q1_nport.zip"
NPORT_DIR = "nport_data"
 
# ---- EDIT THESE if the column names differ after you inspect the actual TSVs ----
# IMPORTANT: these are the SEC's documented field names, but casing/naming has
# changed across dataset versions. get_aum_flows() prints the columns it actually
# finds on first run — check that output against these and adjust if needed.
COL_FUND_NAME = "SERIES_NAME"
COL_TOTAL_ASSETS = "TOTAL_ASSETS"
COL_ACCESSION = "ACCESSION_NUMBER"
COL_PERIOD = "REPORT_ENDING_PERIOD"
 
# Real monthly flow fields (N-PORT Item B.6). Net flow = sales + reinvestment - redemption.
# This is the correct way to measure flows; differencing total assets (the old method)
# blends flows with market moves. If these columns aren't present we fall back to the
# old difference method and print a warning so the number is never silently wrong.
FLOW_COLS = {
    "sales":        ["SALES_FLOW_MON1", "SALES_FLOW_MON2", "SALES_FLOW_MON3"],
    "reinvestment": ["REINVESTMENT_FLOW_MON1", "REINVESTMENT_FLOW_MON2", "REINVESTMENT_FLOW_MON3"],
    "redemption":   ["REDEMPTION_FLOW_MON1", "REDEMPTION_FLOW_MON2", "REDEMPTION_FLOW_MON3"],
}
 
 
# ---------------------------------------------------------------------------
# Networking helpers: throttle + retry so we stay friendly with SEC's servers.
# ---------------------------------------------------------------------------
_last_request_at = 0.0
 
 
def _check_headers():
    """Fail fast with a clear message if the User-Agent hasn't been edited."""
    if "your-email@example.com" in HEADERS.get("User-Agent", ""):
        raise RuntimeError(
            "SEC will block requests until you set a real User-Agent. "
            "Edit the HEADERS line near the top of pipeline.py to your name and email."
        )
 
 
def sec_get(url, params=None, stream=False, timeout=30):
    """
    GET a SEC URL with global throttling and automatic backoff/retry.
    Returns the requests.Response (caller decides whether to raise_for_status).
    """
    global _last_request_at
    last_exc = None
    for attempt in range(SEC_MAX_RETRIES):
        wait = SEC_MIN_INTERVAL - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()
        try:
            resp = requests.get(url, headers=HEADERS, params=params, stream=stream, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            time.sleep(2 ** attempt)  # exponential backoff on network errors
            continue
        # Retry on rate-limit / forbidden / server errors
        if resp.status_code in (429, 403) or resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp  # exhausted retries; hand back the last response for the caller to inspect
 
 
# ---------------------------------------------------------------------------
# EDGAR full-text search
# ---------------------------------------------------------------------------
def edgar_search(form_type, start_date, end_date, from_=0, size=EFTS_PAGE_SIZE):
    """One page of EDGAR results for a form type + date range.
 
    We filter ONLY by form type and date — NOT by a full-text keyword. Earlier the
    query searched the document text for the literal form code (e.g. "485BPOS"),
    which only matched the few filings that happen to contain that string and badly
    undercounted. EDGAR accepts a filing-type-only search, so this returns every
    filing of the form in the window.
    """
    url = "https://efts.sec.gov/LATEST/search-index"  # note: /LATEST/ is case-sensitive
    params = {
        "forms": form_type,
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "from": from_,
        "size": size,
    }
    r = sec_get(url, params=params)
    r.raise_for_status()
    return r.json()
 
 
def _iter_all_hits(form_type, start_date, end_date):
    """
    Yield every hit for a form type across ALL pages, not just the first one.
    Stops at SEC's 10,000-result cap.
    """
    from_ = 0
    total = None
    while True:
        data = edgar_search(form_type, start_date, end_date, from_=from_, size=EFTS_PAGE_SIZE)
        hits = data.get("hits", {})
        page = hits.get("hits", [])
        if total is None:
            total = hits.get("total", {}).get("value", 0)
        if not page:
            break
        for hit in page:
            yield hit
        # Advance by how many we ACTUALLY got, not the requested size. EDGAR may return
        # fewer per page than requested; using len(page) guarantees no records are skipped.
        from_ += len(page)
        if from_ >= total or from_ >= EFTS_RESULT_CAP:
            break
 
 
def _has_term(name, term):
    """
    True if `term` appears in `name` as a whole word/phrase, not as a substring.
    This is the core fix: "ai" matches "AI" but NOT "sustainable"; "space" matches
    "Space ETF" but NOT "Aerospace". Terms with symbols (e.g. "s&p") fall back to
    plain containment, since word boundaries don't behave around non-word chars.
    """
    if any(not (c.isalnum() or c.isspace()) for c in term):
        return term in name
    pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
    return re.search(pattern, name) is not None
 
 
def _score_buckets(name):
    """Count how many distinct keywords each bucket matches. Returns {bucket: score}."""
    name = str(name).lower()
    return {b: sum(_has_term(name, kw) for kw in kws) for b, kws in CATEGORY_RULES.items()}
 
 
def categorize(fund_name):
    """Returns the best-fitting bucket string (highest keyword score, else 'other')."""
    scores = _score_buckets(fund_name)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"
 
 
def bucket_confidence(fund_name):
    """
    'high'   — one bucket clearly wins,
    'medium' — a winner exists but another bucket also matched (ambiguous name),
    'low'    — nothing matched (fell through to 'other').
    Lets analysts filter to labels they can trust.
    """
    scores = _score_buckets(fund_name)
    ordered = sorted(scores.values(), reverse=True)
    if ordered[0] == 0:
        return "low"
    return "high" if ordered[0] > ordered[1] else "medium"
 
 
def get_fund_type(fund_name):
    """Returns 'ETF' or 'Mutual Fund' based on name and known brand signals."""
    name = str(fund_name).lower()
    if any(_has_term(name, kw) for kw in ETF_KEYWORDS) or any(b in name for b in ETF_BRANDS):
        return "ETF"
    return "Mutual Fund"
 
 
def get_management_style(fund_name, fund_type):
    """Returns 'Active', 'Passive', or 'Unknown' — only meaningful for ETFs."""
    if fund_type != "ETF":
        return None
    name = str(fund_name).lower()
    if any(_has_term(name, kw) for kw in PASSIVE_KEYWORDS):
        return "Passive"
    if any(_has_term(name, kw) for kw in ACTIVE_KEYWORDS):
        return "Active"
    return "Unknown"
 
 
def get_industry(fund_name):
    """Returns an industry label — only meaningful for the 'thematic' bucket."""
    name = str(fund_name).lower()
    for label, terms in INDUSTRY_RULES.items():
        if any(_has_term(name, t) for t in terms):
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
        r = sec_get(url, timeout=15)
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
 
 
def _accession_from_hit(hit):
    """EDGAR hit _id looks like 'accession:filename'. Pull out the accession number."""
    hit_id = hit.get("_id", "") or ""
    if ":" in hit_id:
        return hit_id.split(":", 1)[0]
    # Fallback: some responses carry the accession under _source["adsh"]
    return hit.get("_source", {}).get("adsh") or None
 
 
def get_new_filings(start_date, end_date):
    """
    Pull every new filing across all requested form types and pages.
    A failure on one form type is logged and skipped so the rest still return.
    """
    rows = []
    for form in FORM_TYPES:
        try:
            for hit in _iter_all_hits(form, start_date, end_date):
                src = hit.get("_source", {})
                fund_name = ", ".join(src.get("display_names", []))
                ciks = src.get("ciks", [])
                cik0 = ciks[0] if ciks else None
                accession = _accession_from_hit(hit)
 
                fund_type = get_fund_type(fund_name)
                management_style = get_management_style(fund_name, fund_type)
                bucket = categorize(fund_name)
                bucket_conf = bucket_confidence(fund_name)
                industry = get_industry(fund_name) if bucket == "thematic" else None
                description = None
                if bucket == "thematic":
                    description = get_filing_description(cik0, hit.get("_id"))
 
                # Fallback key if SEC ever omits the accession, so dedup still works.
                if not accession:
                    accession = f"{src.get('file_date')}|{form}|{fund_name}"
 
                rows.append({
                    "accession_number": accession,
                    "fund_name": fund_name,
                    "form_type": src.get("root_forms", [form])[0] if src.get("root_forms") else form,
                    "filing_date": src.get("file_date"),
                    "filer_cik": ", ".join(ciks) if ciks else None,
                    "fund_type": fund_type,
                    "management_style": management_style,
                    "bucket": bucket,
                    "bucket_confidence": bucket_conf,
                    "industry": industry,
                    "description": description,
                })
        except Exception as e:
            print(f"  ! Skipped form {form} after an error: {e}")
 
    df = pd.DataFrame(rows)
    if not df.empty:
        # Dedup within this run; the DB's UNIQUE constraint handles cross-run dedup.
        df = df.drop_duplicates(subset=["accession_number"])
    return df
 
 
def download_and_extract_nport():
    """Downloads the N-PORT zip (if not already present) and extracts it."""
    zip_path = "nport.zip"
    if not os.path.exists(zip_path):
        r = sec_get(NPORT_ZIP_URL, stream=True, timeout=120)
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded {zip_path}")
 
    if not os.path.exists(NPORT_DIR):
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(NPORT_DIR)
        print(f"Extracted into {NPORT_DIR}/")
 
 
def _flow_columns_present(df):
    """True only if every configured monthly flow column exists in the dataframe."""
    needed = [c for group in FLOW_COLS.values() for c in group]
    return all(c in df.columns for c in needed)
 
 
def get_aum_flows():
    """
    Parses the downloaded N-PORT TSVs and returns a DataFrame for the aum_flows table
    with a `net_flow` column = sales + reinvestment - redemption, summed over the three
    reported months (N-PORT Item B.6). This is real investor money in/out.
 
    If the flow columns aren't found (dataset naming varies by version), it falls back
    to differencing total net assets and clearly warns that the figure is an approximation.
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
 
        # Self-documenting: show what's actually in the file so you can verify COL_* names.
        print(f"FUND_REPORTED_INFO columns ({len(fund_info.columns)}): "
              f"{list(fund_info.columns)[:25]}{' ...' if len(fund_info.columns) > 25 else ''}")
 
        merged = fund_info.merge(
            submissions[[COL_ACCESSION, COL_PERIOD]], on=COL_ACCESSION, how="left"
        )
 
        keep = [COL_FUND_NAME, COL_TOTAL_ASSETS, COL_PERIOD]
        use_real_flows = _flow_columns_present(merged)
        if use_real_flows:
            keep += [c for group in FLOW_COLS.values() for c in group]
 
        merged = merged[keep].copy()
        merged = merged.rename(columns={
            COL_FUND_NAME: "fund_name",
            COL_TOTAL_ASSETS: "total_net_assets",
            COL_PERIOD: "period",
        })
        merged["period"] = pd.to_datetime(merged["period"], errors="coerce")
        merged = merged.dropna(subset=["fund_name", "period"])
 
        if use_real_flows:
            for group in FLOW_COLS.values():
                for c in group:
                    merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0)
            sales = merged[FLOW_COLS["sales"]].sum(axis=1)
            reinv = merged[FLOW_COLS["reinvestment"]].sum(axis=1)
            redem = merged[FLOW_COLS["redemption"]].sum(axis=1)
            merged["net_flow"] = sales + reinv - redem
            print("AUM: using real reported net flows (sales + reinvestment - redemption).")
        else:
            print("AUM WARNING: flow columns not found — falling back to total-asset "
                  "differences (an approximation that includes market moves, not just flows). "
                  "Check the printed column list above and update FLOW_COLS.")
            merged = merged.sort_values(["fund_name", "period"])
            merged["net_flow"] = merged.groupby("fund_name")["total_net_assets"].diff()
 
        merged["bucket"] = merged["fund_name"].apply(categorize)
        return merged[["fund_name", "total_net_assets", "period", "net_flow", "bucket"]]
    except Exception as e:
        print(f"Failed to parse N-PORT data: {e}")
        return pd.DataFrame()
 
 
def setup_db(conn):
    # accession_number is UNIQUE so repeated runs can't create duplicate filings.
    conn.execute("""CREATE TABLE IF NOT EXISTS filings
        (accession_number TEXT UNIQUE, fund_name TEXT, form_type TEXT, filing_date TEXT,
         filer_cik TEXT, fund_type TEXT, management_style TEXT, bucket TEXT,
         bucket_confidence TEXT, industry TEXT, description TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS aum_flows
        (fund_name TEXT, total_net_assets REAL, period TEXT, net_flow REAL, bucket TEXT)""")
    conn.commit()
 
 
def upsert_filings(conn, df):
    """Insert filings, skipping any accession numbers already stored. Returns count added."""
    cols = ["accession_number", "fund_name", "form_type", "filing_date", "filer_cik",
            "fund_type", "management_style", "bucket", "bucket_confidence", "industry", "description"]
    records = [tuple(row.get(c) for c in cols) for row in df.to_dict("records")]
 
    before = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    placeholders = ",".join(["?"] * len(cols))
    conn.executemany(
        f"INSERT OR IGNORE INTO filings ({','.join(cols)}) VALUES ({placeholders})",
        records,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    return after - before
 
 
def run_all(start_date, end_date):
    """Pulls fresh filings for the given date range and writes them into SQLite.
    Also attempts to ingest N-PORT AUM data (quarterly; safe to run repeatedly —
    it skips the download if the zip is already present)."""
    _check_headers()
    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)
 
    # ---- Filings (guarded so a fetch error can't take down the whole run) ----
    try:
        filings_df = get_new_filings(start_date, end_date)
        if not filings_df.empty:
            added = upsert_filings(conn, filings_df)
            print(f"Fetched {len(filings_df)} filings; {added} new (rest were already stored).")
        else:
            print("No new filings found for this range.")
    except Exception as e:
        print(f"Filings fetch failed: {e}")
 
    # ---- AUM flows (already guarded; quarterly, best-effort) ----
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
 
