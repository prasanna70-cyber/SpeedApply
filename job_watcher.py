#!/usr/bin/env python3
"""
Job watcher: checks company career pages (Greenhouse / Lever / Ashby) for fresh India jobs.
New matching job -> queued for auto_apply.py (if APPLICANT_PROFILE secret is set), else Telegram alert.

  python3 job_watcher.py           # one scan
  python3 job_watcher.py --check   # test which company names in config.json work
  python3 job_watcher.py --test    # send a test Telegram message
  python3 job_watcher.py --report  # send today's IST summary to Telegram
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
SEEN, LOG, QUEUE = BASE / "seen.json", BASE / "log.jsonl", BASE / "apply_queue.json"
IST = ZoneInfo("Asia/Kolkata")
UA = {"User-Agent": "Mozilla/5.0 (job-watcher)"}

SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|manager|director|head|architect|vp|ii|iii|iv)\b", re.I)
EXP_A = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|\u2013)?\s*\d{0,2}\s*\+?\s*years?[^.]{0,40}experience", re.I)
EXP_B = re.compile(r"experience[^.]{0,30}?(\d{1,2})\s*\+?\s*(?:-|to|\u2013)?\s*\d{0,2}\s*\+?\s*years?", re.I)
NO_REGION = re.compile(r"^\W*(remote|anywhere|global|worldwide|flexible|n/?a)?\W*$", re.I)

DEFAULT_INDIA_TERMS = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
    "mumbai", "delhi", "ncr", "gurugram", "gurgaon", "noida",
    "kolkata", "ahmedabad",
]

# job titles that match a keyword like "cloud" but are not engineering jobs
DEFAULT_EXCLUDE = [
    "sales", "account executive", "account manager", "marketing", "recruiter", "recruiting",
    "talent", "legal", "counsel", "finance", "accountant", "designer", "customer success",
    "business development", "partnerships", "product manager", "people operations", "hr",
]


def now_ist() -> datetime:
    return datetime.now(IST)


def get_json(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&nbsp;", " ")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def has_word(text, word):
    """Whole-word match, so 'india' does not match 'Indiana' and 'intern' does not match 'international'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", text.lower()) is not None


# ---- fetchers: each returns a list of {id,title,location,url,desc} ----
def fetch_greenhouse(token):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    return [
        {
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "desc": strip_html(j.get("content", "")),
        }
        for j in d.get("jobs", [])
    ]


def fetch_lever(token):
    d = get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    return [
        {
            "id": j["id"],
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "desc": j.get("descriptionPlain", "") or "",
        }
        for j in d
    ]


def fetch_ashby(token):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    return [
        {
            "id": j["id"],
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", ""),
            "desc": j.get("descriptionPlain", "") or "",
        }
        for j in d.get("jobs", [])
    ]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ---- profile helpers ----
def profile():
    try:
        return json.loads(os.environ.get("APPLICANT_PROFILE") or "{}")
    except Exception:
        return {}


def my_years(cfg):
    try:
        return float(profile().get("experience_years", cfg.get("experience_years", 0)))
    except Exception:
        return 0.0


def day_budget():
    """How many more applications may be queued today (None = no daily limit)."""
    try:
        limit = int(profile().get("max_per_day") or 0)
    except Exception:
        limit = 0
    if limit <= 0:
        return None
    today = now_ist().date().isoformat()
    used = 0
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("time", "").startswith(today) and r.get("action") == "applied":
                used += 1
    return max(0, limit - used)


# ---- matching ----
def work_mode(job):
    t = (job["location"] + " " + job["title"] + " " + job["desc"][:1000]).lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t or "work from home" in t or "wfh" in t:
        return "remote"
    return "onsite"


def in_india(job, cfg):
    terms = cfg.get("india_terms") or DEFAULT_INDIA_TERMS
    loc = job["location"].lower()
    if any(has_word(loc, t) for t in terms):
        return True
    # location empty or just "Remote": look at the title and the start of the description
    if NO_REGION.match(loc):
        text = job["title"] + " " + job["desc"][:1500]
        return any(has_word(text, t) for t in terms)
    return False


def required_years(desc):
    """Highest 'N years experience' number mentioned in the job text, or None."""
    nums = [int(m.group(1)) for m in list(EXP_A.finditer(desc)) + list(EXP_B.finditer(desc))]
    return max(nums) if nums else None


def matches(job, cfg):
    title = job["title"]
    roles = cfg.get("roles") or []
    if roles and not any(has_word(title, r) for r in roles):
        return False
    if any(has_word(title, x) for x in cfg.get("exclude_titles", DEFAULT_EXCLUDE)):
        return False
    if cfg.get("india_only", True) and not in_india(job, cfg):
        return False
    allowed_modes = [m.lower() for m in cfg.get("work_modes", ["remote", "hybrid", "onsite"])]
    if work_mode(job) not in allowed_modes:
        return False
    if cfg.get("experience", "match_profile") == "match_profile":
        mine = my_years(cfg)
        tol = float(cfg.get("experience_tolerance", 1))
        need = required_years(job["desc"])
        if need is not None and need > mine + tol:
            return False
        if mine < 5 and SENIOR.search(title):
            return False
    return True


# ---- telegram & logging ----
def notify(cfg, text):
    t = cfg.get("telegram", {})
    if not t.get("bot_token") or not t.get("chat_id") or "PASTE" in t["bot_token"]:
        print("[telegram not configured]", text[:80])
        return False
    data = urllib.parse.urlencode({
        "chat_id": t["chat_id"],
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", data=data, headers=UA),
            timeout=20,
        ).read()
        return True
    except Exception as e:
        print("[telegram failed]", e)
        return False


def log(entry):
    entry["time"] = now_ist().isoformat(timespec="seconds")
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---- commands ----
def scan(cfg):
    queue_mode = bool(os.environ.get("APPLICANT_PROFILE"))
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    new_seen, queue = set(seen), []

    apply_existing = bool(cfg.get("apply_existing", False))
    cap = int(cfg.get("max_queue_per_run", 5))
    if queue_mode:
        budget = day_budget()
        if budget is not None:
            cap = min(cap, budget)

    def grab(c):
        try:
            return c, FETCHERS[c["ats"]](c["token"]), None
        except Exception as e:
            return c, None, e

    with ThreadPoolExecutor(32) as ex:
        results = list(ex.map(grab, cfg.get("companies", [])))

    for c, jobs, err in results:
        if err:
            print(f"[skip {c['token']}] {err}")
            continue
        prefix = f"{c['ats']}:{c['token']}:"
        seeding = not any(k.startswith(prefix) for k in seen)
        for j in jobs:
            key = prefix + str(j["id"])
            if key in seen:
                continue
            if seeding and not apply_existing:
                new_seen.add(key)
                continue
            j.update(
                company=c.get("name", c["token"]),
                ats=c["ats"],
                token=c["token"],
                mode=work_mode(j),
            )
            if not matches(j, cfg):
                new_seen.add(key)
                continue
            if len(queue) >= cap:
                # over this run's limit: leave it unseen so a later run picks it up
                continue
            new_seen.add(key)
            ts = now_ist().strftime("%d %b %Y, %I:%M:%S %p IST")
            if queue_mode:
                j["desc"] = j["desc"][:3000]
                queue.append(j)
                print("QUEUED", j["company"], "-", j["title"])
            else:
                queue.append(j)  # counts toward the per-run cap
                msg = (
                    f"NEW INDIA JOB [{j['mode'].upper()}]\n"
                    f"Role: {j['title']}\n"
                    f"Company: {j['company']}\n"
                    f"Location: {j['location'] or 'Not specified'}\n"
                    f"Found: {ts}\n"
                    f"Link: {j['url']}"
                )
                notify(cfg, msg)
                log({
                    "company": j["company"],
                    "title": j["title"],
                    "url": j["url"],
                    "mode": j["mode"],
                    "action": "notified",
                })
        if seeding and not apply_existing:
            print(f"[{c['token']}] first time: recorded {len(jobs)} existing jobs, no alerts. New postings will be handled.")

    SEEN.write_text(json.dumps(sorted(new_seen)))
    if queue and queue_mode:
        QUEUE.write_text(json.dumps(queue))


def check(cfg):
    for c in cfg.get("companies", []):
        try:
            count = len(FETCHERS[c["ats"]](c["token"]))
            print(f"OK   {c['ats']:<10} {c['token']:<30} {count} jobs")
        except Exception as e:
            print(f"FAIL {c['ats']:<10} {c['token']:<30} {e}")


def report(cfg):
    today = now_ist().date().isoformat()
    rows = [json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []
    rows = [r for r in rows if r.get("time", "").startswith(today)]
    applied = [r for r in rows if r.get("action") == "applied"]
    dry = [r for r in rows if r.get("action") == "dry_run"]
    manual = [r for r in rows if r.get("action") not in ("applied", "dry_run")]
    modes = {m: sum(1 for r in rows if r.get("mode") == m) for m in ("remote", "hybrid", "onsite")}
    target = int(cfg.get("daily_target", 10))
    text = (
        f"Daily IST Report ({today})\n"
        f"Auto-applied: {len(applied)} / target {target}\n"
        f"Manual needed: {len(manual)}\n"
        f"Dry run: {len(dry)}\n"
        f"Matched jobs: {len(rows)} (remote {modes['remote']}, hybrid {modes['hybrid']}, onsite {modes['onsite']})\n"
        + "\n".join(f"- {r['title']} @ {r['company']} -> {r['url']}" for r in manual[:10])
    )
    print(text)
    notify(cfg, text)


def test_bot(cfg):
    ts = now_ist().strftime("%d %b %Y, %I:%M:%S %p IST")
    companies_count = len(cfg.get("companies", []))
    msg = (
        f"🟢 BOT IS RUNNING & ACTIVE\n"
        f"Tracked Companies: {companies_count}\n"
        f"Schedule: Every 15 mins + 9:00 PM IST Daily Report\n"
        f"Checked at: {ts}"
    )
    print("sent" if notify(cfg, msg) else "not sent")


if __name__ == "__main__":
    cfg = json.loads((BASE / "config.json").read_text())
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram"] = {
            "bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        }
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    {
        "--check": check,
        "--report": report,
        "--test": test_bot,
    }.get(arg, scan)(cfg)
