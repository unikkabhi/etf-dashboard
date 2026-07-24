"""
explore_korea.py — ONE-OFF exploration of South Korea's DART Open API.

Why this exists: before building a real Korea pipeline, we need to SEE what DART
actually returns for fund / ETF disclosures — the filing categories, the report
titles, the language, and the volume. DART is built around listed-company
disclosures, so how new Korean funds appear is an open question. This script just
prints what comes back; it writes nothing and changes nothing.

How to run it:
  - Locally:  export DART_API_KEY="your-key"   then   python explore_korea.py
  - Or via the explore_korea.yml GitHub Action (reads the DART_API_KEY secret),
    then read the output in the Action log and paste it back.

DART status codes you might see:
  000 = OK · 010 = unregistered key · 011 = unauthorized/again later ·
  013 = no data found · 020 = request limit exceeded · 100 = bad parameter ·
  800/900 = system error
"""

import os
import datetime
import requests

API_KEY = os.environ.get("DART_API_KEY", "")
LIST_URL = "https://opendart.fss.or.kr/api/list.json"


def fetch(params):
    p = {"crtfc_key": API_KEY}
    p.update(params)
    r = requests.get(LIST_URL, params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def show(title, params):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    try:
        data = fetch(params)
    except Exception as e:
        print(f"  request failed: {e}")
        return
    status = data.get("status")
    print(f"  status={status}  message={data.get('message')}")
    if status != "000":
        return
    print(f"  total_count={data.get('total_count')}  total_page={data.get('total_page')}")
    rows = data.get("list", [])
    print(f"  showing up to 15 of {len(rows)} rows on this page:")
    for row in rows[:15]:
        print(f"    [{row.get('rcept_dt')}] cls={row.get('corp_cls')} "
              f"| {row.get('corp_name')} | {row.get('report_nm')} | filer={row.get('flr_nm')}")


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("DART_API_KEY is not set. Add it as a secret / environment variable first.")

    today = datetime.date.today()
    end = today.strftime("%Y%m%d")
    bgn7 = (today - datetime.timedelta(days=7)).strftime("%Y%m%d")
    bgn30 = (today - datetime.timedelta(days=30)).strftime("%Y%m%d")
    print(f"DART exploration · today={end}")

    # 1) ALL disclosures, last 7 days — confirms the key works and shows overall volume.
    show("1) ALL disclosures, last 7 days (page 1)",
         {"bgn_de": bgn7, "end_de": end, "page_no": 1, "page_count": 100})

    # 2) FUND disclosures only. pblntf_ty=G is DART's 'fund disclosure' category.
    show("2) FUND disclosures (pblntf_ty=G), last 7 days",
         {"bgn_de": bgn7, "end_de": end, "pblntf_ty": "G", "page_no": 1, "page_count": 100})

    # 3) Same, widened to 30 days in case a single week is sparse.
    show("3) FUND disclosures (pblntf_ty=G), last 30 days",
         {"bgn_de": bgn30, "end_de": end, "pblntf_ty": "G", "page_no": 1, "page_count": 100})

    print("\nDone. Copy this ENTIRE output back so we can design the real Korea pipeline "
          "around what DART actually returns.")
