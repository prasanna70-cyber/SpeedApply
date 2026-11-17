#!/usr/bin/env python3
"""
Job watcher: checks company career pages (Greenhouse / Lever / Ashby) every few minutes.
New matching job -> Telegram alert within one polling interval, and (optionally) email application.

Usage:
  python3 job_watcher.py           # one scan (run this from cron every 2 minutes)
  python3 job_watcher.py --check   # test which company names in config.json actually work
  python3 job_watcher.py --test    # send a test Telegram message
  python3 job_watcher.py --report  # send today's summary to Telegram
Only Python 3.8+ standard library is needed.
"""
import json, re, smtplib, ssl, sys, urllib.parse, urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).parent
SEEN, LOG = BASE / "seen.json", BASE / "log.jsonl"
UA = {"User-Agent": "Mozilla/5.0 (job-watcher)"}
SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|manager|director|head|architect|vp)\b", re.I)
FRESHER = re.compile(r"\b(fresher|freshers|intern|internship|trainee|graduate|entry[- ]level|junior|jr\.?|associate|0\s*-\s*[12]\s*years?)\b", re.I)
YEARS = re.compile(r"\b([3-9]|1\d)\s*\+?\s*(?:-\s*\d+\s*)?\+?\s*years?", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


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
    return [{"id": str(j["id"]), "title": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""), "desc": strip_html(j.get("content", ""))} for j in d.get("jobs", [])]


def fetch_lever(token):
    d = get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    return [{"id": j["id"], "title": j.get("text", ""), "location": (j.get("categories") or {}).get("location", "") or "",
             "url": j.get("hostedUrl", ""), "desc": j.get("descriptionPlain", "") or ""} for j in d]


def fetch_ashby(token):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    return [{"id": j["id"], "title": j.get("title", ""), "location": j.get("location", "") or "",
             "url": j.get("jobUrl", ""), "desc": j.get("descriptionPlain", "") or ""} for j in d.get("jobs", [])]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ---- matching ----
def work_mode(job):
    t = (job["location"] + " " + job["title"] + " " + job["desc"][:800]).lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t or "work from home" in t or "wfh" in t:
        return "remote"
    return "onsite"


def in_india(job, cfg):
    terms = [x.lower() for x in cfg.get("india_terms", [])]
    t = (job["location"] + " " + job["desc"][:600]).lower()
    return any(x in t for x in terms)


def matches(job, cfg):
    title = job["title"].lower()
    if not any(r.lower() in title for r in cfg["roles"]):
        return False
    if cfg.get("india_only", True) and not in_india(job, cfg):
        return False
    if work_mode(job) not in [m.lower() for m in cfg.get("work_modes", ["remote", "hybrid", "onsite"])]:
        return False
    if cfg.get("fresher_only", True):
        if SENIOR.search(job["title"]) or YEARS.search(job["desc"]):
            return False
    return True


def find_apply_email(job):
    for e in EMAIL.findall(job["desc"]):
        if not re.search(r"no-?reply|example\.|sentry|@\d", e, re.I):
            return e
    return None


# ---- actions ----
def notify(cfg, text):
    """Send a Telegram message through your own bot (free)."""
    t = cfg.get("telegram", {})
    if not t.get("bot_token") or not t.get("chat_id") or "PASTE" in t["bot_token"]:
        print("[telegram not configured]", text[:80]); return False
    data = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", data=data, headers=UA), timeout=20).read()
        return True
    except Exception as e:
        print("[telegram failed]", e); return False


def send_application(cfg, job, to_addr):
    m = cfg["email"]
    msg = EmailMessage()
    msg["From"], msg["To"] = m["address"], to_addr
    msg["Subject"] = f"Application for {job['title']} - {m.get('name', '')}".strip(" -")
    msg.set_content(m["body"].replace("{title}", job["title"]).replace("{company}", job["company"]))
    cv = Path(m["cv_path"]).expanduser()
    msg.add_attachment(cv.read_bytes(), maintype="application", subtype="pdf", filename=cv.name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(m["address"], m["app_password"])
        s.send_message(msg)


def log(entry):
    entry["time"] = datetime.now().isoformat(timespec="seconds")
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---- commands ----
def scan(cfg):
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    new_seen = set(seen)
    for c in cfg["companies"]:
        try:
            jobs = FETCHERS[c["ats"]](c["token"])
        except Exception as e:
            print(f"[skip {c['token']}] {e}"); continue
        prefix = f"{c['ats']}:{c['token']}:"
        seeding = not any(k.startswith(prefix) for k in seen)  # first time we see this company: record only, no alerts
        for j in jobs:
            key = prefix + str(j["id"])
            if key in seen:
                continue
            new_seen.add(key)
            if seeding:
                continue
            j["company"] = c.get("name", c["token"])
            if not matches(j, cfg):
                continue
            to = find_apply_email(j)
            action = "notified"
            if to and cfg.get("auto_email", {}).get("enabled") and not cfg["email"].get("dry_run", True):
                try:
                    send_application(cfg, j, to); action = "emailed"
                except Exception as e:
                    action = f"email_failed: {e}"
            elif to:
                action = "notified (email found, dry_run on)"
            msg = (f"NEW JOB [{work_mode(j).upper()}]: {j['title']} @ {j['company']}\n{j['location']}\n{j['url']}\n"
                   + ("Auto-applied by email to " + to if action == "emailed" else "Apply manually: tap the link now"))
            notify(cfg, msg)
            log({"company": j["company"], "title": j["title"], "url": j["url"], "mode": work_mode(j), "action": action})
            print("MATCH", j["company"], "-", j["title"], "->", action)
        if seeding:
            print(f"[{c['token']}] first time: recorded {len(jobs)} existing jobs, no alerts. New postings will alert you.")
    SEEN.write_text(json.dumps(sorted(new_seen)))


def check(cfg):
    for c in cfg["companies"]:
        try:
            print(f"OK   {c['ats']:<10} {c['token']:<30} {len(FETCHERS[c['ats']](c['token']))} jobs")
        except Exception as e:
            print(f"FAIL {c['ats']:<10} {c['token']:<30} {e}")


def report(cfg):
    today = datetime.now().date().isoformat()
    rows = [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []
    rows = [r for r in rows if r["time"].startswith(today)]
    emailed = [r for r in rows if r["action"] == "emailed"]
    manual = [r for r in rows if r["action"] != "emailed"]
    modes = {m: sum(1 for r in rows if r.get("mode") == m) for m in ("remote", "hybrid", "onsite")}
    text = (f"Daily report {today}\nMatched: {len(rows)} (remote {modes['remote']}, hybrid {modes['hybrid']}, onsite {modes['onsite']})\nAuto-applied by email: {len(emailed)}\nNeed manual apply: {len(manual)}\n"
            + "\n".join(f"- {r['title']} @ {r['company']} {r['url']}" for r in manual[:10]))
    print(text); notify(cfg, text)


if __name__ == "__main__":
    cfg = json.loads((BASE / "config.json").read_text())
    import os  # secrets come from GitHub Secrets (env vars) so nothing private sits in the repo
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram"] = {"bot_token": os.environ["TELEGRAM_BOT_TOKEN"], "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "")}
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    {"--check": check, "--report": report, "--test": lambda c: print("sent" if notify(c, "Job watcher test: Telegram works.") else "not sent")}.get(arg, scan)(cfg)
