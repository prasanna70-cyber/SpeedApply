JOB WATCHER + AUTO-APPLY (free, runs 24/7 on GitHub, nothing to install)

FILES IN THIS FOLDER
  job_watcher.py        upload to repo
  auto_apply.py         upload to repo
  config.json           upload to repo, then edit roles + companies
  watch.yml             do NOT upload. Create .github/workflows/watch.yml on GitHub and paste this text
  APPLICANT_PROFILE.json  do NOT upload. Edit with your real details, paste into a GitHub Secret

PROCESS
 1. Telegram: @BotFather > /newbot > copy BOT TOKEN. Open your bot, press Start, send "hi".
    Open https://api.telegram.org/bot<TOKEN>/getUpdates and copy the number after "chat":{"id":
 2. github.com > New repository > name job-watcher > PUBLIC > create.
 3. Add file > Upload files: job_watcher.py, auto_apply.py, config.json (unzipped, not the zip) > Commit.
 4. Add file > Create new file > name  .github/workflows/watch.yml  > paste watch.yml > Commit.
 5. In your bot chat in Telegram: send your CV PDF, press and hold it, choose Pin.
 6. Repo > Settings > Secrets and variables > Actions > New repository secret (3 times):
      TELEGRAM_BOT_TOKEN  = your bot token
      TELEGRAM_CHAT_ID    = your chat id number
      APPLICANT_PROFILE   = the edited APPLICANT_PROFILE.json text
 7. Edit config.json on GitHub: roles, companies. Company name = from careers link
      boards.greenhouse.io/NAME  or job-boards.greenhouse.io/NAME  -> ats "greenhouse"
      jobs.lever.co/NAME                                            -> ats "lever"
      jobs.ashbyhq.com/NAME                                         -> ats "ashby"
 8. Actions tab > job-watcher > Run workflow: mode "test" (Telegram message arrives), then mode "check"
    (OK/FAIL per company; delete FAIL lines from config.json).
 9. It now runs every 5 minutes by itself. First sight of a company only records its existing jobs.
    You get DRY RUN messages (forms filled, NOT submitted). When they look right: Settings > Secrets >
    APPLICANT_PROFILE > Update > paste the whole JSON again with "dry_run": false. Now it submits for real.

TELEGRAM MESSAGES
  APPLIED            submitted automatically (full job details included)
  APPLY MANUALLY     could not be done safely (CAPTCHA, email code, unknown required question, closed job);
                     contains company, title, location, mode, apply link and job summary
  Daily report       9 PM IST
  To teach it a question it keeps asking: add ["keyword","answer"] to "answers" in the secret.

LIMITS
  Only companies on Greenhouse / Lever / Ashby. GitHub schedule can be a few minutes late.
  Some forms always need you (CAPTCHA, email codes). Tested on mock forms, not on live company sites.
  If alerts stop after 60 idle days, press Run workflow once.
