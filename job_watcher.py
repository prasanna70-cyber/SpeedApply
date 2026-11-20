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

SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|manager|director|head|architect|vp)\b", re.I)
EXP_A = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|\u2013)?\s*\d{0,2}\s*\+?\s*years?[^.]{0,40}experience", re.I)
EXP_B = re.compile(r"experience[^.]{0,30}?(\d{1,2})\s*\+?\s*(?:-|to|\u2013)?\s*\d{0,2}\s*\+?\s*years?", re.I)

DEFAULT_INDIA_TERMS = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
    "mumbai", "delhi", "ncr", "gurugram", "gurgaon", "noida",
    "kolkata", "ahmedabad", "remote - india", "apac", "ist"
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


# ---- matching ----
def work_mode(job):
    t = (job["location"] + " " + job["title"] + " " + job["desc"][:1000]).lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t or "work from home" in t or "wfh" in t:
        return "remote"
    return "onsite"


def in_india(job, cfg):
    terms = [x.lower() for x in (cfg.get("india_terms") or DEFAULT_INDIA_TERMS)]
    t = (job["location"] + " " + job["desc"][:2000]).lower()
    return any(x in t for x in terms)


def required_years(desc):
    """Highest 'N years experience' number mentioned in the job text, or None."""
    nums = [int(m.group(1)) for m in list(EXP_A.finditer(desc)) + list(EXP_B.finditer(desc))]
    return max(nums) if nums else None


def my_years(cfg):
    try:
        profile = json.loads(os.environ.get("APPLICANT_PROFILE") or "{}")
        return float(profile.get("experience_years", cfg.get("experience_years", 0)))
    except Exception:
        return 0.0


def matches(job, cfg):
    title = job["title"].lower()
    roles = cfg.get("roles") or []
    if roles and not any(r.lower() in title for r in roles):
        return False
    if cfg.get("india_only", True) and not in_india(job, cfg):
        return False
    allowed_modes = [m.lower() for m in cfg.get("work_modes", ["remote", "hybrid", "onsite"])]
    if work_mode(job) not in allowed_modes:
        return False
    if cfg.get("experience", "match_profile") == "match_profile":
        mine = my_years(cfg)
        need = required_years(job["desc"])
        if need is not None and need > mine + 1:
            return False
        if mine < 5 and SENIOR.search(job["title"]):
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
            new_seen.add(key)
            if seeding:
                continue
            j.update(
                company=c.get("name", c["token"]),
                ats=c["ats"],
                token=c["token"],
                mode=work_mode(j),
            )
            if not matches(j, cfg):
                continue
            ts = now_ist().strftime("%d %b %Y, %I:%M:%S %p IST")
            if queue_mode:
                j["desc"] = j["desc"][:3000]
                queue.append(j)
                print("QUEUED", j["company"], "-", j["title"])
            else:
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
        if seeding:
            print(f"[{c['token']}] first time: recorded {len(jobs)} existing jobs, no alerts. New postings will be handled.")

    SEEN.write_text(json.dumps(sorted(new_seen)))
    if queue:
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
    text = (
        f"Daily IST Report ({today})\n"
        f"Matched jobs: {len(rows)} (remote {modes['remote']}, hybrid {modes['hybrid']}, onsite {modes['onsite']})\n"
        f"Auto-applied: {len(applied)}\n"
        f"Dry run: {len(dry)}\n"
        f"Manual / notified: {len(manual)}\n"
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
