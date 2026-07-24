"""
pipeline_korea.py — South Korea (DART) fund-filing ingestion.

Fetches collective-investment (fund) disclosures from Korea's DART Open API,
separates genuine NEW REGISTRATIONS from routine issuance reports, adds a
best-effort English rendering of the Korean filing text via a regulatory-term
glossary (deterministic and free — no external translation service), and stores
everything in the `filings_korea` table of etf_data.db.

The US pipeline (pipeline.py) is untouched; Korea lives in its own table so the
two can coexist and the dashboard can offer a country selector.

Requires the DART_API_KEY environment variable (free key from opendart.fss.or.kr).
Run:  python pipeline_korea.py
"""

import os
import time
import sqlite3
import datetime
from collections import Counter

import requests

DB_PATH = "etf_data.db"
API_KEY = os.environ.get("DART_API_KEY", "")
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

FUND_TYPE_CODE = "G"  # DART's 'fund disclosure' category

# ---- Best-effort Korean -> English glossary (fund-filing vocabulary) ----
# Deterministic and free. Translates standard regulatory terms; unique fund
# names remain in Korean (reliable free translation of proper nouns isn't feasible).
GLOSSARY = {
    "증권신고서": "Securities Registration Statement",
    "투자설명서": "Investment Prospectus",
    "증권발행실적보고서": "Securities Issuance Performance Report",
    "집합투자증권": "Collective Investment Securities",
    "상장지수집합투자기구": "ETF",
    "상장지수": "Exchange-Traded",
    "투자신탁": "Investment Trust",
    "자산운용": "Asset Management",
    "정정": "(Amended)",
    "주식혼합": "Equity-Mixed",
    "채권혼합": "Bond-Mixed",
    "혼합": "Mixed",
    "주식": "Equity",
    "채권": "Bond",
    "파생형": "Derivatives-type",
    "재간접": "Fund-of-Funds",
    "부동산": "Real Estate",
    "인프라": "Infrastructure",
    "단기금융": "Money Market",
    "머니마켓": "Money Market",
}


def to_english(text):
    """Swap known Korean regulatory terms for English; leave unique names as-is."""
    if not text:
        return text
    out = text
    for ko, en in GLOSSARY.items():
        out = out.replace(ko, en)
    return out


def classify_nature(report_nm):
    """Separate new fund launches from routine paperwork, by Korean report type."""
    name = report_nm or ""
    if "증권신고서" in name:            # Securities Registration Statement = new offering
        return "New registration"
    if "투자설명서" in name:            # Investment Prospectus
        return "Prospectus"
    if "증권발행실적보고서" in name:    # post-issuance performance report = routine
        return "Issuance report (routine)"
    return "Other fund filing"


def is_etf(report_nm):
    name = (report_nm or "").upper()
    return 1 if ("상장지수" in (report_nm or "") or "ETF" in name) else 0


def _get(params):
    p = {"crtfc_key": API_KEY}
    p.update(params)
    r = requests.get(LIST_URL, params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_fund_filings(start_date, end_date):
    """Paginate all fund (pblntf_ty=G) disclosures in the window."""
    rows, page, total_page = [], 1, 1
    while page <= total_page:
        data = _get({"bgn_de": start_date, "end_de": end_date,
                     "pblntf_ty": FUND_TYPE_CODE, "page_no": page, "page_count": 100})
        status = data.get("status")
        if status != "000":
            print(f"DART status={status} message={data.get('message')}")
            break
        total_page = data.get("total_page", 1)
        rows.extend(data.get("list", []))
        page += 1
        time.sleep(0.3)  # be gentle with DART
    return rows


def build_records(raw_rows):
    recs, seen = [], set()
    for it in raw_rows:
        rcept_no = it.get("rcept_no")
        if not rcept_no or rcept_no in seen:
            continue
        seen.add(rcept_no)
        report_ko = it.get("report_nm", "")
        corp_ko = it.get("corp_name", "")
        rcept_dt = it.get("rcept_dt", "")
        filing_date = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}" if len(rcept_dt) == 8 else None
        recs.append({
            "rcept_no": rcept_no,
            "country": "South Korea",
            "filing_date": filing_date,
            "corp_name_ko": corp_ko,
            "corp_name_en": to_english(corp_ko),
            "report_nm_ko": report_ko,
            "report_nm_en": to_english(report_ko),
            "filing_nature": classify_nature(report_ko),
            "is_etf": is_etf(report_ko),
            "filer": it.get("flr_nm"),
            "corp_cls": it.get("corp_cls"),
            "dart_url": DART_VIEW + rcept_no,
        })
    return recs


def setup_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS filings_korea
        (rcept_no TEXT UNIQUE, country TEXT, filing_date TEXT,
         corp_name_ko TEXT, corp_name_en TEXT, report_nm_ko TEXT, report_nm_en TEXT,
         filing_nature TEXT, is_etf INTEGER, filer TEXT, corp_cls TEXT, dart_url TEXT)""")
    conn.commit()


def upsert(conn, recs):
    cols = ["rcept_no", "country", "filing_date", "corp_name_ko", "corp_name_en",
            "report_nm_ko", "report_nm_en", "filing_nature", "is_etf", "filer",
            "corp_cls", "dart_url"]
    before = conn.execute("SELECT COUNT(*) FROM filings_korea").fetchone()[0]
    conn.executemany(
        f"INSERT OR IGNORE INTO filings_korea ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})",
        [tuple(r[c] for c in cols) for r in recs],
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM filings_korea").fetchone()[0]
    return after - before


def run_all(start_date, end_date):
    if not API_KEY:
        raise SystemExit("DART_API_KEY is not set.")
    recs = build_records(fetch_fund_filings(start_date, end_date))

    # ---- self-verifying breakdown (read this in the Action log) ----
    nat = Counter(r["filing_nature"] for r in recs)
    print(f"\nKorea fund filings {start_date}..{end_date}: {len(recs)} unique")
    for k, v in nat.most_common():
        print(f"  {k}: {v}")
    print("  ETF-tagged:", sum(r["is_etf"] for r in recs))
    print("\n  Sample NEW REGISTRATIONS (up to 10):")
    shown = 0
    for r in recs:
        if r["filing_nature"] == "New registration":
            print(f"    [{r['filing_date']}] {r['report_nm_ko']}")
            print(f"        EN: {r['report_nm_en']}")
            shown += 1
            if shown >= 10:
                break
    if shown == 0:
        print("    (none in this category — registrations may live outside pblntf_ty=G; "
              "we'll widen the query if so)")

    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)
    added = upsert(conn, recs)
    print(f"\nWrote {added} new Korea rows to {DB_PATH} (filings_korea table).")
    conn.close()


if __name__ == "__main__":
    today = datetime.date.today()
    start = today - datetime.timedelta(days=30)  # 30-day window for a fuller first pull
    run_all(start.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
