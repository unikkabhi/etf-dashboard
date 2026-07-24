Indxx — ETF & Fund Filings Dashboard

A research dashboard that tracks newly filed U.S. ETF and mutual fund registration documents from SEC EDGAR, classifies them, and presents them for product-research and index-development work. Data refreshes automatically every day.

Note: the classifications in this tool are heuristics intended as a research starting point, not a system of record. Every filing links back to its original document on SEC EDGAR so it can be verified at the source. See Limitations before relying on any figure.

What it does
Tracks new filings of forms S-1, N-1A, and 485BPOS from SEC EDGAR over a rolling window, deduplicated by accession number.
Classifies each filing by fund type (ETF vs Mutual Fund), management style (Active / Passive), category (digital, fixed income, derivatives, thematic, equity), and thematic industry — each with a confidence flag.
Separates signal from noise. Flags each filing's nature — New registration, Routine amendment, or bulk Unit-Investment-Trust (UIT) paperwork — and surfaces a dedicated "Likely new fund launches" view.
Shows AUM net flows by category from quarterly SEC N-PORT data (real reported flows: sales + reinvestment − redemptions), in US dollars.
Stays honest about freshness. A status bar shows when the data was last updated and warns if it has gone stale.
Links every row to EDGAR and supports CSV export of the current view.
How it works
SEC EDGAR  ──►  pipeline.py  ──►  etf_data.db (SQLite)  ──►  app.py (Streamlit)
(filings +      (fetch,            (stored, deduplicated)     (dashboard)
 N-PORT)         classify, store)
pipeline.py fetches filings from the EDGAR full-text search API and quarterly N-PORT data, classifies them, and writes to a local SQLite database.
app.py is the Streamlit dashboard that reads that database and renders it.
A GitHub Actions workflow runs the pipeline daily and commits the refreshed database back to the repository, so the deployed dashboard stays current without anyone running anything by hand.
Repository structure
File	Purpose
app.py	The Streamlit dashboard (UI, charts, filters, tables).
pipeline.py	Data ingestion, classification, and SQLite storage.
etf_data.db	The SQLite database (rebuilt daily by the workflow).
indxx_logo.png	Logo used in the dashboard header.
requirements.txt	Python dependencies.
.github/workflows/	The scheduled daily-update workflow.
.streamlit/config.toml	(Optional) theme configuration.
Running it locally

Requires Python 3.12.

bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your SEC contact email (SEC blocks generic requests)
#    Use your real name and email.
export SEC_EMAIL="Your Name your-email@example.com"

# 3. Build the database (first run downloads N-PORT data; this can take a while)
python pipeline.py

# 4. Launch the dashboard
streamlit run app.py

The dashboard opens in your browser and reads etf_data.db. Re-run python pipeline.py any time to pull the latest filings.

Automated daily updates

The workflow in .github/workflows/ runs every day at 06:00 UTC (and can be triggered manually from the Actions tab → Run workflow). It fetches the latest filings, updates etf_data.db, and commits it back to the repo.

Required secret: the SEC requires a real contact email in the request header. Add it as a repository secret so it is never committed to the code:

Settings → Secrets and variables → Actions → New repository secret
Name: SEC_EMAIL · Value: Your Name your-email@example.com

If deploying on Streamlit Community Cloud, add the same secret there (Manage app → Settings → Secrets) in TOML form:

toml
SEC_EMAIL = "Your Name your-email@example.com"
Limitations & data caveats

This tool is a research aid. Please keep the following in mind before using any output for decision-making:

Classifications are heuristic. Fund type, category, style, industry, and filing-nature labels are inferred largely from the fund's name via keyword matching. They are accurate for clear cases and uncertain for ambiguous ones — hence the confidence flag and the "high-confidence only" toggle. They are not authoritative.
New-launch detection is a shortlist, not a guarantee. A filing is inferred to be a "new registration" from its form type, not by confirming the fund did not previously exist. Treat the launches view as a high-quality list to review, and verify via the linked EDGAR document.
Filings data has a lag. EDGAR full-text search typically indexes a filing the next business day, so same-day filings may not appear until the following day.
AUM / flow data is quarterly and delayed. N-PORT data is released quarterly and lagged (~60 days), and only covers funds that are already operating — never brand-new filings.
Form coverage is limited to S-1, N-1A, and 485BPOS. Other filing types are not currently tracked.

Every row links to the original SEC filing so figures can always be confirmed at the source.

Data sources
SEC EDGAR Full-Text Search — filing metadata.
SEC Form N-PORT structured data sets — fund net flows and assets.
SEC press releases RSS — the in-dashboard news panel (live, agency press releases).

All data is public and sourced from the U.S. Securities and Exchange Commission (sec.gov).

Internal research tool. Built for the Indxx product-research team.
