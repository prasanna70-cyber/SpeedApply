#!/usr/bin/env python3
"""
Auto-applier. Reads apply_queue.json (written by job_watcher.py), opens each application form in a
headless browser, fills every required field from your profile and submits.
If anything cannot be filled safely (unknown required question, CAPTCHA, code check, error) it does NOT
guess: it sends a Telegram message with the complete job details so you can apply by hand.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
QUEUE, LOG, CV = BASE / "apply_queue.json", BASE / "log.jsonl", BASE / "cv.pdf"
IST = ZoneInfo("Asia/Kolkata")
UA = {"User-Agent": "Mozilla/5.0 (job-watcher)"}
TOKEN, CHAT = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")

try:
    P = json.loads(os.environ.get("APPLICANT_PROFILE") or "{}")
except Exception:
    P = {}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SEL = (
    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset]),"
    " select, textarea"
)


def now_ist() -> datetime:
    return datetime.now(IST)


# ---------------- telegram ----------------
def tg(text):
    if not TOKEN or not CHAT:
        print(text)
        return
    data = urllib.parse.urlencode({
        "chat_id": CHAT,
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, headers=UA),
            timeout=20,
        ).read()
    except Exception as e:
        print("[telegram failed]", e)


def tg_api(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}?" + urllib.parse.urlencode(params)
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read())


def fetch_cv():
    """Your CV = the PDF you PIN in the chat with your bot."""
    if CV.exists():
        return True
    try:
        fid = P.get("cv_file_id") or tg_api("getChat", chat_id=CHAT)["result"]["pinned_message"]["document"]["file_id"]
        path = tg_api("getFile", file_id=fid)["result"]["file_path"]
        CV.write_bytes(
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/file/bot{TOKEN}/{path}", headers=UA), timeout=60).read()
        )
        return True
    except Exception as e:
        print("[cv download failed]", e)
        return False


# ---------------- answers ----------------
def fmt(s, job):
    return str(s).replace("{title}", job["title"]).replace("{company}", job["company"])


def answer_for(label, job):
    l = label.lower().strip()
    for kw, ans in P.get("answers", []):
        if kw.lower() in l:
            return fmt(ans, job)
    full = f"{P.get('first_name', '')} {P.get('last_name', '')}".strip()
    g = P.get
    rules = [
        (r"first name|given name|forename|preferred name|nickname", g("first_name")),
        (r"last name|surname|family name", g("last_name")),
        (r"full name|your name|legal name|^name\b", full),
        (r"e-?mail", g("email")),
        (r"phone|mobile|contact number|telephone", g("phone")),
        (r"linkedin", g("linkedin")),
        (r"github", g("github")),
        (r"portfolio|website|personal (site|url)|other (url|link)", g("portfolio") or g("github") or g("linkedin")),
        (r"authori[sz]ed to work|legally (authori[sz]ed|eligible|permitted)|right to work|work authori[sz]ation|eligible to work", "Yes"),
        (r"sponsor", "No"),
        (r"relocat", "Yes"),
        (r"current (company|employer)|present employer|most recent (company|employer)|employer", g("current_company", "Fresher")),
        (r"current (job )?title|current role|current designation|job title|designation", g("current_title", "Fresher")),
        (r"notice", g("notice_period", "Immediate")),
        (r"current (ctc|salary|compensation)|present (ctc|salary)", g("current_ctc", "0")),
        (r"expected|desired|salary expectation|expectation", g("expected_ctc")),
        (r"years? of (relevant |total |work |professional )?experience|total experience|how many years|experience in years", g("experience_years", "0")),
        (r"college|university|institution|school", g("college")),
        (r"degree|qualification", g("degree")),
        (r"graduation|passing year|year of (passing|graduation)", g("graduation_year")),
        (r"cgpa|gpa|percentage|marks", g("cgpa")),
        (r"\bstate\b|province", g("state")),
        (r"country", g("country", "India")),
        (r"city|location|where are you (based|located)|residence|address", g("city")),
        (r"cover letter|why .*(join|interested|want|apply)|tell us about yourself|additional information|anything else", g("cover_letter")),
        (r"how did you (hear|find)|where did you (hear|find)|source", g("source", "Job board")),
        (r"start date|available to start|availability|joining|when can you", g("notice_period", "Immediate")),
    ]
    for pat, val in rules:
        if re.search(pat, l):
            return fmt(val, job) if val not in (None, "") else None
    return None


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def pick_option(opts, ans, label=""):
    a = norm(ans)
    texts = [(i, norm(t)) for i, t in opts if norm(t) and not re.match(r"^(select|choose|please select|--)", norm(t))]
    for i, t in texts:
        if t == a:
            return i
    if a in ("yes", "no"):
        for i, t in texts:
            if t.startswith(a + " ") or t.startswith(a + ",") or t.startswith(a + "."):
                return i
    else:
        for i, t in texts:
            if a and (a in t or (len(t) > 2 and t in a)):
                return i
    if re.search(r"hear|source|find out", label.lower()):
        for i, t in texts:
            if re.search(r"other|job board|website|internet|linkedin|online", t):
                return i
    return None


def decline_option(opts):
    for i, t in opts:
        if re.search(r"decline|prefer not|do not wish|don.t wish|not to (say|disclose|answer)|choose not|rather not|no answer", norm(t)):
            return i
    return None


# ---------------- page helpers ----------------
FIELDS_JS = r"""
els => els.map((e,i)=>{
  const r=e.getBoundingClientRect(), cs=getComputedStyle(e);
  const visible=!(cs.display==='none'||cs.visibility==='hidden')&&r.width>0&&r.height>0;
  const clean=t=>(t||'').replace(/\s+/g,' ').trim();
  let lab='';
  if(e.labels&&e.labels.length) lab=[...e.labels].map(x=>x.innerText).join(' ');
  if(!lab&&e.getAttribute('aria-labelledby')) lab=e.getAttribute('aria-labelledby').split(' ').map(id=>{const n=document.getElementById(id);return n?n.innerText:''}).join(' ');
  if(!lab) lab=e.getAttribute('aria-label')||'';
  if(!lab) lab=e.placeholder||'';
  if(!lab){let p=e.parentElement;for(let k=0;k<3&&p&&!lab;k++,p=p.parentElement){const ls=p.querySelectorAll('label'),cs=p.querySelectorAll('input,select,textarea');if(ls.length===1&&cs.length===1)lab=ls[0].innerText;}}
  if(!lab) lab=e.name||e.id||'';
  let q=lab, optlab='';
  if(e.type==='radio'){
    optlab=(e.labels&&e.labels[0]?e.labels[0].innerText:e.value)||'';
    const opts=[...document.querySelectorAll('input[type=radio]')].filter(x=>x.name===e.name).map(x=>clean(x.labels&&x.labels[0]?x.labels[0].innerText:''));
    q='';
    let p=e.parentElement;
    for(let k=0;k<6&&p&&!q;k++,p=p.parentElement){
      const lg=p.querySelector(':scope > legend'); if(lg&&clean(lg.innerText)){q=lg.innerText;break;}
      const lines=p.innerText.split(/\r?\n/).map(clean).filter(Boolean).filter(s=>!opts.includes(s));
      if(lines.length) q=lines[0];
    }
    q=q||e.name;
  }
  return {i,tag:e.tagName.toLowerCase(),type:(e.type||'').toLowerCase(),name:e.name||'',label:clean(q),optlabel:clean(optlab),
    required:!!(e.required||e.getAttribute('aria-required')==='true'||/\*|✱|\(required\)/i.test(q)),
    visible,disabled:!!e.disabled,role:e.getAttribute('role')||'',auto:e.getAttribute('aria-autocomplete')||'',
    value:e.value||'',options:e.tagName==='SELECT'?[...e.options].map(o=>clean(o.text)):[]};
})
"""

CAPTCHA_JS = r"""
big => [...document.querySelectorAll('iframe')].some(f=>{
  const s=(f.src||'')+' '+(f.title||''); if(!/captcha|turnstile/i.test(s)) return false;
  if(!big&&/size=invisible/i.test(s)) return false;
  const r=f.getBoundingClientRect(), cs=getComputedStyle(f);
  return cs.visibility!=='hidden'&&cs.display!=='none'&&r.width>(big?200:100)&&r.height>(big?200:50);
})
"""

SUCCESS = re.compile(r"thank you for (applying|your (application|interest))|application (has been |was )?(submitted|received|sent)|we.?ve received your application|successfully submitted|thanks for applying", re.I)
ERRORS = re.compile(r"this field is required|is required|please (fill|complete|enter|select|provide)|verification code|security code|enter the .{0,20}code|unable to submit|something went wrong|try again", re.I)
CLOSED = re.compile(r"no longer (available|accepting)|position (has been )?(filled|closed)|job (not found|is closed|has been closed)|page (was )?not found|couldn.t find that job", re.I)


def apply_url(job):
    if job.get("apply_url"):
        return job["apply_url"]
    u, ats = job["url"], job["ats"]
    if ats == "greenhouse":
        return f"https://boards.greenhouse.io/{job['token']}/jobs/{job['id']}"
    if ats == "lever":
        return u if u.rstrip("/").endswith("/apply") else u.rstrip("/") + "/apply"
    if ats == "ashby":
        return u if u.rstrip("/").endswith("/application") else u.rstrip("/") + "/application"
    return u


def details(job, reason="", extra_meta=""):
    desc = (job.get("desc") or "")[:700].strip()
    mail = EMAIL.findall(job.get("desc") or "")
    ts = now_ist().strftime("%d %b %Y, %I:%M:%S %p IST")
    return (
        f"{job['title']} @ {job['company']}\n"
        f"Mode: {job['mode'].upper()}  |  Location: {job['location'] or 'Not specified'}\n"
        f"Time: {ts}\n"
        + (f"{extra_meta}\n" if extra_meta else "")
        + f"Apply link: {apply_url(job)}\nJob page: {job['url']}\n"
        + (f"Apply email in post: {mail[0]}\n" if mail else "")
        + (f"Reason: {reason}\n" if reason else "")
        + f"\nAbout the job:\n{desc}..."
    )


def read_fields(page):
    return page.eval_on_selector_all(SEL, FIELDS_JS)


def submit_button(page):
    best = None
    cands = page.locator("button[type=submit], input[type=submit], button, a[role=button]")
    for i in range(cands.count()):
        b = cands.nth(i)
        try:
            if not b.is_visible():
                continue
            t = (b.get_attribute("value") or "") if b.evaluate("e=>e.tagName") == "INPUT" else b.inner_text()
        except Exception:
            continue
        t = norm(t)
        if re.search(r"submit|apply|send application", t) and not re.search(r"linkedin|indeed|autofill|with |google|seek|upload|attach|resume", t):
            best = b
    return best


# ---------------- filling ----------------
def fill_form(page, job):
    """returns (filled_labels, missing_labels)"""
    fields = [f for f in read_fields(page) if not f["disabled"] and (f["visible"] or f["type"] == "file")]
    filled, missing = [], []
    radios = {}
    for f in fields:
        if f["type"] == "radio":
            radios.setdefault(f["name"], []).append(f)
    done_radio = set()
    for f in fields:
        loc = page.locator(SEL).nth(f["i"])
        label, req, t = f["label"], f["required"], f["type"]
        try:
            if t == "file":
                if re.search(r"cover", label, re.I):
                    if req:
                        missing.append(label or "cover letter file")
                    continue
                if re.search(r"resume|cv|attach|upload|^$", label, re.I) or len([x for x in fields if x["type"] == "file"]) == 1:
                    loc.set_input_files({
                        "name": P.get("resume_name", "Resume.pdf"),
                        "mimeType": "application/pdf",
                        "buffer": CV.read_bytes(),
                    })
                    filled.append("resume")
                elif req:
                    missing.append(label or "file upload")
                continue
            if t == "radio":
                if f["name"] in done_radio:
                    continue
                done_radio.add(f["name"])
                grp = radios[f["name"]]
                ans = answer_for(label, job)
                opts = [(x["i"], x["optlabel"]) for x in grp]
                idx = pick_option(opts, ans, label) if ans else None
                if idx is None and any(x["required"] for x in grp):
                    idx = decline_option(opts)
                if idx is None:
                    if any(x["required"] for x in grp):
                        missing.append(label)
                    continue
                page.locator(SEL).nth(idx).check(force=True)
                filled.append(label[:40])
                continue
            if t == "checkbox":
                if re.search(r"agree|consent|acknowledge|privacy|terms|i (have read|confirm|certify|understand)|accurate|true and correct", label, re.I) or req:
                    if re.search(r"agree|consent|acknowledge|privacy|terms|confirm|certify|understand|accurate|read|correct", label, re.I):
                        loc.check(force=True)
                        filled.append("consent")
                    else:
                        missing.append(label)
                continue
            if f["tag"] == "select":
                ans = answer_for(label, job)
                opts = list(enumerate(f["options"]))
                idx = pick_option(opts, ans, label) if ans else None
                if idx is None and req:
                    idx = decline_option(opts)
                if idx is None:
                    if req:
                        missing.append(label)
                    continue
                loc.select_option(index=idx)
                filled.append(label[:40])
                continue
            if f["value"].strip():
                continue
            ans = answer_for(label, job)
            if not ans:
                if req:
                    missing.append(label)
                continue
            if f["role"] == "combobox" or f["auto"]:
                loc.click()
                loc.fill("")
                loc.type(ans[:40], delay=25)
                page.wait_for_timeout(900)
                opt = page.locator("[role=option]").first
                if opt.count() and opt.is_visible():
                    opt.click()
                filled.append(label[:40])
                continue
            loc.fill(ans)
            filled.append(label[:40])
        except Exception as e:
            print(f"[field error] {label}: {str(e)[:100]}")
            if req:
                missing.append(label or "unknown field")
    return filled, missing


def apply_one(browser, job):
    """returns (status, reason, filled) ; status = applied | dry_run | manual"""
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        viewport={"width": 1280, "height": 1800},
        locale="en-IN",
    )
    page = ctx.new_page()
    page.set_default_timeout(15000)
    try:
        page.goto(apply_url(job), wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        body = page.inner_text("body")
        if CLOSED.search(body[:3000]):
            return "manual", "job page says it is closed or not found", []
        if page.locator("input[type=email]").count() == 0:
            b = page.get_by_role("button", name=re.compile(r"^apply( now| for this job)?$", re.I)).first
            if b.count() == 0:
                b = page.get_by_role("link", name=re.compile(r"^apply( now| for this job)?$", re.I)).first
            if b.count():
                b.click()
                page.wait_for_timeout(2500)
        if page.locator("input[type=email]").count() == 0:
            return "manual", "no application form found on the page", []
        if page.evaluate(CAPTCHA_JS, False):
            return "manual", "CAPTCHA on the form (cannot be solved automatically)", []
        filled, missing = fill_form(page, job)
        if missing:
            return "manual", "required fields I could not fill safely: " + "; ".join(sorted(set(missing)))[:300], filled
        if P.get("dry_run", True):
            return "dry_run", "form filled but NOT submitted (dry run is ON)", filled
        btn = submit_button(page)
        if not btn:
            return "manual", "submit button not found", filled
        btn.click()
        page.wait_for_timeout(7000)
        if page.evaluate(CAPTCHA_JS, True):
            return "manual", "CAPTCHA challenge appeared after submit", filled
        body = page.inner_text("body")
        if SUCCESS.search(body) or re.search(r"confirm|thank|submitted|success", page.url, re.I):
            return "applied", "", filled
        m = ERRORS.search(body)
        if m:
            return "manual", "form showed: " + m.group(0), filled
        return "manual", "submitted but I could not confirm success, please check", filled
    except Exception as e:
        return "manual", "error: " + str(e)[:150], []
    finally:
        ctx.close()


# ---------------- queue ----------------
def log_row(job, action):
    with LOG.open("a") as f:
        f.write(json.dumps({
            "time": now_ist().isoformat(timespec="seconds"),
            "company": job["company"],
            "title": job["title"],
            "url": job["url"],
            "mode": job["mode"],
            "action": action,
        }) + "\n")


def applied_today():
    if not LOG.exists():
        return 0
    today = now_ist().date().isoformat()
    n = 0
    for line in LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("time", "").startswith(today) and r.get("action") == "applied":
            n += 1
    return n


def save_queue(q):
    if q:
        QUEUE.write_text(json.dumps(q))
    elif QUEUE.exists():
        QUEUE.unlink()


def manual(job, reason):
    tg("APPLY MANUALLY\n" + details(job, reason))
    log_row(job, "manual: " + reason[:80])


def main():
    q = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
    if not q:
        return
    if "--fallback" in sys.argv:
        for j in q:
            manual(j, "auto-apply could not run")
        save_queue([])
        return

    from playwright.sync_api import sync_playwright

    have_cv = fetch_cv()
    raw_limit = P.get("max_per_day", 0)
    limit = int(raw_limit) if raw_limit else 0  # 0 = no limit
    run_applied = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        while q:
            job = q[0]
            today_count = applied_today()
            if not have_cv:
                manual(job, "CV not found: send your CV PDF to the bot and PIN that message")
            elif limit > 0 and today_count >= limit:
                manual(job, f"daily limit of {limit} applications reached")
            else:
                status, reason, filled = apply_one(browser, job)
                if status == "applied":
                    run_applied += 1
                    total_now = today_count + 1
                    tg(
                        "APPLIED (submitted automatically)\n"
                        + details(job, extra_meta=f"Run Count: #{run_applied}  |  Today Total: {total_now}")
                        + "\n\nFilled: "
                        + ", ".join(filled[:12])
                    )
                    log_row(job, "applied")
                elif status == "dry_run":
                    tg(
                        "DRY RUN - filled, NOT submitted\n"
                        + details(job)
                        + "\n\nFilled: "
                        + ", ".join(filled[:12])
                    )
                    log_row(job, "dry_run")
                else:
                    manual(job, reason)
            q.pop(0)
            save_queue(q)
        browser.close()


if __name__ == "__main__":
    main()
