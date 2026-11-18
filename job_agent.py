"""
Job Alert Agent (0-1 year, CSE roles, Karnataka + Hyderabad)

Sites     : LinkedIn, Indeed, Naukri, Google Jobs
Freshness : HOURS_OLD (env, default 3 for scheduled runs; use 24 for the first run)
Experience: shows 0-1 year jobs and jobs with unclear experience (marked); drops 2+ years / senior
Applicants: drops jobs with >= MAX_APPLICANTS where the site shows a count (LinkedIn, Naukri)
Memory    : seen_jobs.json so you never get the same job twice
Alerts    : Telegram (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID as env vars / GitHub secrets)

Usage:  python job_agent.py          # one search run
        python job_agent.py --test   # send a test Telegram message
"""
import os, re, json, sys, time, random
import pandas as pd
import requests
from jobspy import scrape_jobs

# ------------------------- CONFIG -------------------------
SEARCH_TERMS = [
    "software engineer", "software developer", "associate software engineer",
    "full stack developer", "backend developer", "frontend developer",
    "android developer", "flutter developer", "react native developer", "ios developer",
    "automation engineer", "qa automation engineer", "sdet",
    "python developer", "java developer",
    "data analyst", "data engineer", "machine learning engineer", "ai engineer",
    "rpa developer", "salesforce developer", "servicenow developer", "sap developer",
    "graduate engineer trainee", "fresher software developer",
]
LOCATIONS = ["Bengaluru, Karnataka", "Hyderabad, Telangana", "Karnataka, India"]
SITES = ["linkedin", "indeed", "naukri", "google"]
HOURS_OLD = int(os.getenv("HOURS_OLD", 3))
MAX_APPLICANTS = 100
RESULTS_PER_SEARCH = 30
MAX_ALERTS_PER_RUN = 40          # extra jobs wait for the next run

LOCATION_OK = re.compile(
    r"bengaluru|bangalore|karnataka|hyderabad|secunderabad|mysuru|mysore|mangaluru|mangalore|"
    r"hubballi|hubli|belagavi|belgaum|telangana", re.I)
TITLE_OK = re.compile(
    r"software|developer|engineer|\bsde\b|programmer|full.?stack|front.?end|back.?end|android|\bios\b|"
    r"flutter|react|node|python|java|automation|sdet|\bqa\b|rpa|uipath|salesforce|servicenow|\bsap\b|"
    r"data analyst|data engineer|data scientist|machine learning|\bml\b|\bai\b|genai|trainee|graduate", re.I)
TITLE_BAD = re.compile(
    r"devops|cyber|security|\bsre\b|site reliability|cloud|network|support (engineer|analyst)|"
    r"mechanical|civil|electrical|sales|marketing|\bhr\b|recruiter|accountant|driver", re.I)
SENIOR = re.compile(
    r"\bsenior\b|\bsr\.?\b|\blead\b|\bmanager\b|\bprincipal\b|\bstaff\b|\barchitect\b|\bhead\b|"
    r"\bdirector\b|\bsde.?(2|3|ii|iii|iv)\b|engineer (ii|iii|iv)\b|\bvp\b", re.I)
FRESHER = re.compile(
    r"fresher|freshers|entry.?level|trainee|graduate|junior|\bsde.?(1|i)\b|0\s*[-–to]+\s*1|"
    r"campus|new grad|early career", re.I)
RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+|-|–|to)\s*(\d+(?:\.\d+)?)?\s*(?:\+\s*)?(?:years?|yrs?)", re.I)
EXP_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|professional\s+|work\s+|hands.on\s+)?experience", re.I)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
SEEN_FILE = "seen_jobs.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
# ----------------------------------------------------------


def telegram(text):
    if not (TOKEN and CHAT):
        print("Telegram not configured; message:\n", text)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT, "text": text[:4000], "disable_web_page_preview": True},
                          timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("Telegram failed:", e)
        return False


def collect():
    frames = []
    for term in SEARCH_TERMS:
        for loc in LOCATIONS:
            try:
                df = scrape_jobs(
                    site_name=SITES, search_term=term,
                    google_search_term=f"{term} jobs near {loc} since yesterday",
                    location=loc, results_wanted=RESULTS_PER_SEARCH,
                    hours_old=HOURS_OLD, country_indeed="India",
                )
                n = 0 if df is None else len(df)
                if n:
                    frames.append(df)
                print(f"[ok] {term} | {loc} -> {n}")
            except Exception as e:
                print(f"[skip] {term} | {loc}: {str(e)[:120]}")
            time.sleep(random.uniform(1, 2))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["job_url"])


def clean(df):
    df = df.copy()
    for col in ["title", "company", "location", "description", "experience_range"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    df = df[df["location"].str.contains(LOCATION_OK)]
    df = df[df["title"].str.contains(TITLE_OK) & ~df["title"].str.contains(TITLE_BAD)]
    return df


def experience_status(row):
    """Returns 'fit', 'unclear' or 'drop'."""
    title = row["title"]
    if SENIOR.search(title):
        return "drop"
    rng = row["experience_range"] or ""
    desc = row["description"] or ""
    m = RANGE_RE.search(rng) or RANGE_RE.search(title) or EXP_RE.search(desc) or RANGE_RE.search(desc)
    if m:
        lo = float(m.group(1))
        if lo < 15:                       # ignore things like "20 years of experience in the industry"
            return "fit" if lo <= 1 else "drop"
    if FRESHER.search(f"{title} {rng} {desc[:1500]}"):
        return "fit"
    return "unclear"


def applicant_count(url):
    """Best effort: LinkedIn / Naukri pages. Returns int or None."""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.status_code != 200:
            return None
        t = r.text
        for pat in (r"[Bb]e among the first\s+(\d+)\s+applicants",
                    r"[Oo]ver\s+(\d[\d,]*)\s+applicants",
                    r"(\d[\d,]*)\s+applicants?",
                    r"Applicants?\s*[:\-]?\s*(\d[\d,]*)"):
            m = re.search(pat, t)
            if m:
                return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


def add_applicants(df):
    counts = []
    for _, row in df.iterrows():
        if str(row.get("site", "")) in ("linkedin", "naukri"):
            counts.append(applicant_count(row["job_url"]))
            time.sleep(random.uniform(1, 2))
        else:
            counts.append(None)
    df = df.copy()
    df["applicants"] = counts
    return df


def load_seen():
    try:
        return set(json.load(open(SEEN_FILE)))
    except Exception:
        return set()


def format_job(r):
    tag = "✅ 0-1 yr" if r["exp"] == "fit" else "❓ Experience unclear - check before applying"
    a = "not shown" if pd.isna(r["applicants"]) else int(r["applicants"])
    return (f"{tag}\n{r['title']}\n{r['company']} - {r['location']}\n"
            f"Site: {r['site']} | Applicants: {a}\n{r['job_url']}")


def main():
    raw = collect()
    if raw.empty:
        print("No results.")
        return
    df = clean(raw)
    seen = load_seen()
    df = df[~df["job_url"].isin(seen)]
    if df.empty:
        print("Nothing new.")
        return
    candidates = set(df["job_url"])

    df["exp"] = df.apply(experience_status, axis=1)
    df = df[df["exp"] != "drop"]
    df = add_applicants(df)
    df = df[df["applicants"].isna() | (df["applicants"] < MAX_APPLICANTS)]
    df = df.sort_values(["exp", "applicants"], ascending=[True, True], na_position="last")  # 'fit' before 'unclear'

    to_send, overflow = df.head(MAX_ALERTS_PER_RUN), df.iloc[MAX_ALERTS_PER_RUN:]
    for _, r in to_send.iterrows():
        telegram(format_job(r))
        time.sleep(0.7)
    print(f"{len(to_send)} alerts sent, {len(overflow)} waiting for next run")

    seen |= (candidates - set(overflow["job_url"]))
    json.dump(sorted(seen), open(SEEN_FILE, "w"))


if __name__ == "__main__":
    if "--test" in sys.argv:
        ok = telegram("✅ Job agent test: your bot is connected.")
        print("Test message sent." if ok else "Test failed - check token and chat ID.")
    else:
        main()
