# Deploying forex-ai with GitHub Actions + GitHub Pages

## What you end up with

| Thing | Details |
|---|---|
| **Daily automation** | GitHub Actions runs `daily.py` at 6 am NZT every day, for free |
| **Live dashboard** | `https://bodyheath.github.io/forex-ai-dashboard` — open on any device, any time |
| **Persistent data** | `data/trades.csv` and `data/memory.json` are committed back to the repo after each run so nothing is lost between days |
| **Cost** | $0 — public repository, free Actions minutes, free Pages |

Replace `yourusername` with your actual GitHub username throughout this guide.

> **Privacy note:** The main `forex-ai` repository is private — your code and trade history are not publicly visible.
> The dashboard is published to a separate public repository (`forex-ai-dashboard`) so it remains accessible at
> `https://bodyheath.github.io/forex-ai-dashboard`. Your API keys are stored as encrypted Secrets and are
> **never** visible — not even to you in plain text after you save them.

---

## Part 1 — One-time setup

### Step 1 — Create a free GitHub account

Skip this step if you already have one.

1. Go to **github.com** and click **Sign up**.
2. Enter your email address, choose a password, and pick a username.
   Your username becomes part of your dashboard URL, so choose something you are happy sharing.
3. Solve the puzzle, click **Create account**.
4. GitHub sends you a verification email. Open it and click the link.
5. When asked "What kind of work do you do?", you can skip the questions.
6. On the plan selection screen, choose **Free** (it is already selected by default) and continue.

You now have a free GitHub account.

---

### Step 2 — Create a new repository

A repository (repo) is a folder on GitHub that stores your project files and runs the automation.

1. Click the **+** icon in the top-right corner → **New repository**.
2. Set the fields as follows:

   | Field | Value |
   |---|---|
   | Repository name | `forex-ai` |
   | Description | (optional) AI-powered forex pair analyser |
   | Visibility | **Public** ← required for free GitHub Pages |
   | Add a README file | leave **unchecked** |
   | Add .gitignore | leave as **None** |
   | Choose a license | leave as **None** |

3. Click **Create repository**.

GitHub shows you an empty repo with a URL like `https://github.com/yourusername/forex-ai`.
Keep this page open — you will need the URL in Step 3.

---

### Step 3 — Upload the project files

You have two options. Option A uses the Git command-line tool and is the most reliable.
Option B uses GitHub Desktop, a free GUI app with no command line.

---

#### Option A — Git command line (recommended)

**On Windows**, open the Start menu, search for **PowerShell**, and open it.
**On Mac**, open **Terminal** (Applications → Utilities → Terminal).

Check that Git is installed by typing:
```
git --version
```
If you see a version number (e.g. `git version 2.44.0`), you have Git. If you get an error:
- **Windows**: Download Git from git-scm.com/download/win and run the installer (accept all defaults).
- **Mac**: Run `xcode-select --install` in Terminal and follow the prompts.

Then run these commands, replacing `yourusername` with your GitHub username and adjusting the path to where your `forex-ai` folder actually is:

```bash
# Navigate to your project folder
cd "C:\Users\bodyh\Desktop\forex-ai"    # Windows PowerShell
# cd ~/Desktop/forex-ai                 # Mac Terminal

# Set up Git for the first time (only needed once per machine)
git config --global user.name "Your Name"
git config --global user.email "your@email.com"

# Initialise the repo and connect it to GitHub
git init
git remote add origin https://github.com/yourusername/forex-ai.git

# Stage all files, then make the first commit
git add -A
git commit -m "Initial commit"

# Push to GitHub
git push -u origin main
```

If Git asks for your GitHub username and password: for the password, you must use a
**Personal Access Token** (not your account password). To create one:

1. On GitHub, click your profile picture → **Settings** → **Developer settings** (bottom of left sidebar) → **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)**.
2. Give it a note (e.g. "forex-ai upload"), set expiration to **No expiration**, and tick the **repo** checkbox.
3. Click **Generate token**. Copy the token — it shows only once.
4. Paste it as the password when Git prompts you.

---

#### Option B — GitHub Desktop (GUI, no command line)

1. Download **GitHub Desktop** from desktop.github.com and install it.
2. Sign in with your GitHub account.
3. Click **File → Add Local Repository**.
4. Click **Choose...** and navigate to your `forex-ai` folder. Click **Add Repository**.
   (If it says "not a git repository", click **create a repository** instead, set the local path to your folder, and leave all defaults.)
5. Click **Publish repository** in the top bar.
6. In the dialog:
   - Name: `forex-ai`
   - **Uncheck "Keep this code private"** (public is required for free Pages)
   - Click **Publish Repository**.

Your files are now on GitHub.

---

#### What to include / exclude

When uploading, make sure these are included:
```
main.py, daily.py, config.py, requirements.txt, .env.example, .gitignore, DEPLOY.md
prompts/analyst.md
src/  (all .py files)
.github/workflows/daily.yml
docs/index.html, docs/.nojekyll
```

The `.gitignore` file automatically excludes these — do not try to upload them:
```
.env               ← your real API keys (never commit this)
data/cache/        ← thousands of small API response files
*.log              ← verbose run logs
__pycache__/       ← Python bytecode
```

**If you have existing trade history you want to keep**, also include:
```
data/trades.csv
data/memory.json
data/reports/*.txt
```

---

### Step 4 — Add your API keys as GitHub Secrets

Secrets are encrypted environment variables that GitHub injects into the workflow at run time. They are never visible in logs or to other users.

1. On your GitHub repo page, click **Settings** (the gear icon in the top navigation).
2. In the left sidebar, click **Secrets and variables** → **Actions**.
3. Click **New repository secret** and add each key below one at a time:

   | Secret name | Where to find the value |
   |---|---|
   | `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
   | `TWELVE_DATA_KEY` | twelvedata.com → your account dashboard |
   | `NEWS_API_KEY` | newsapi.org → your account |
   | `FRED_API_KEY` | fred.stlouisfed.org/docs/api/api_key.html |
   | `MY_EMAIL` | your email address |

   For each one:
   - Click **New repository secret**
   - Paste the name exactly as shown (case-sensitive)
   - Paste your key as the value
   - Click **Add secret**

4. When done, you should see all five secrets listed. You cannot read the values back — that is intentional.

---

### Step 5 — Enable GitHub Pages

1. On your repo page, click **Settings**.
2. In the left sidebar, click **Pages**.
3. Under **Build and deployment**, set:
   - **Source**: `Deploy from a branch`
   - **Branch**: `main`
   - **Folder**: `/docs`
4. Click **Save**.

GitHub shows a yellow banner. Within a minute or two, it turns green and shows your live URL:

```
https://bodyheath.github.io/forex-ai-dashboard
```

Visiting that URL right now shows the placeholder page ("Dashboard is generated by the daily workflow…"). It will be replaced with the real dashboard after your first workflow run.

---

### Step 6 — Trigger your first run and verify

The workflow is scheduled to run automatically every day, but you can trigger it right now to test everything.

1. On your repo page, click the **Actions** tab.
2. In the left sidebar you will see **Daily Forex Analysis**. Click it.
3. Click **Run workflow** → **Run workflow** (the green button in the dropdown).
4. A new row appears in the list. Click on it to watch the live log.

The run takes **8–12 minutes** because the smart selector paces its Twelve Data API calls to respect the free-tier rate limit (7 calls per minute, with 62-second pauses between batches of 7). This is normal — subsequent same-day runs are faster because the responses are cached, but each fresh daily run always takes this long.

Successful output looks like:
```
[06:00:01] === Daily run 2026-06-04 | universe: 21 pairs ===
[06:00:01] Learning refreshed: 0 closed trades, win rate n/a, 1 auto-patterns written.
[06:00:02] Smart selection: scoring 21 pairs ...
[06:00:02]   Calendar: 2 medium/high-impact events in next 48h
[06:00:02]   (rate-limit pause 62s ...)
...
[06:02:30]   Top 10 selected: GBP/JPY, EUR/USD, AUD/USD, ...
...
[06:09:44] Stage-1 filter: 3/10 pairs screened out, 7 passed to deep analysis.
[06:09:44] Dashboard updated: /home/runner/work/forex-ai/forex-ai/data/dashboard.html
[06:09:45] === Daily run complete ===
```

Once the run finishes (green tick), wait 1–2 minutes for GitHub Pages to rebuild, then visit:

```
https://bodyheath.github.io/forex-ai-dashboard
```

You should see the full live dashboard. **Bookmark this URL on your phone.**

---

## Part 2 — How it works day-to-day

Every day at 6 am NZT, GitHub automatically:

1. **Checks out your repo** — including the latest `trades.csv` and `memory.json`.
2. **Selects the 10 best pairs** — scores all 21 liquid pairs on 24-hour movement, 5-day momentum, and upcoming economic events; picks the top 10.
3. **Screens each pair** — Haiku pre-filters pairs with no real signal (score < 4/5).
4. **Deep-analyses the survivors** — Sonnet runs the full 5-source analysis on the pairs that passed screening.
5. **Pushes results back** — commits the updated `dashboard.html`, `trades.csv`, and `memory.json` to the repo.
6. **Publishes the dashboard** — copies `dashboard.html` to `docs/index.html` so it is live on Pages.

You see the new results by refreshing your bookmarked URL.

### Adjusting for daylight saving

New Zealand observes daylight saving (NZDT, UTC+13) from late September to early April. During that period, 6 am NZT is only 17:00 UTC instead of 18:00 UTC.

To update the cron schedule:
1. Open `.github/workflows/daily.yml` on GitHub (click the file, then the pencil icon).
2. Change `0 18 * * *` to `0 17 * * *`.
3. Click **Commit changes**.

Change it back to `0 18 * * *` in early April when standard time resumes.

---

## Part 3 — Recording outcomes and using the learning system

### Record a trade result

```bash
python main.py --close 7 WIN 1.0925
python main.py --close 7 LOSS
python main.py --close 7 BREAKEVEN
```

After recording, the learning system automatically refreshes and the next daily run will incorporate your track record into the analyst prompt.

If you are not running Python locally, you can edit `data/trades.csv` directly on GitHub (navigate to the file, click the pencil icon) and update the `status`, `exit_price`, and `closed_at` columns manually.

### Check performance stats locally

```bash
python main.py --stats
```

### Analyse a single pair right now (without waiting for the daily run)

```bash
python main.py EUR/USD
```

The result is logged to `trades.csv` and the dashboard is rebuilt locally. To publish it immediately, commit and push the changed files.

---

## Part 4 — Updating the code

```bash
# On your local machine
git add -A
git commit -m "describe what you changed"
git push
```

The next scheduled run will use the updated code automatically.

---

## Troubleshooting

**The workflow shows a red X (failed)**
Click the failed run → click the failed step to see the log.

- `ERROR: missing API keys in .env` — a Secret is missing or mis-named. Re-check Step 4.
  Secret names are case-sensitive: `ANTHROPIC_API_KEY` not `anthropic_api_key`.
- `ModuleNotFoundError` — a package is not in `requirements.txt`. Add it and push.
- `Twelve Data rate limit reached` — you ran too many analyses in a short period. Wait until UTC midnight for the quota to reset.

**The dashboard URL shows "404 Not Found"**
Pages has not built yet. Wait 2 minutes after the first successful run, then refresh.
If it still shows 404, go to Settings → Pages and confirm the branch is `main` and the folder is `/docs`.

**The dashboard shows stale data**
Hard-refresh the page: `Ctrl+Shift+R` (Windows/Linux) or `Cmd+Shift+R` (Mac). Mobile: close the tab completely and reopen the bookmark.

**The workflow did not run at 6 am NZT**
GitHub's scheduler can be up to 1 hour late during periods of high demand. It always runs, just occasionally late. You can always trigger a manual run from the Actions tab if you need results right now.

**I accidentally committed my `.env` file**
Remove it immediately:
```bash
git rm --cached .env
git commit -m "remove .env from tracking"
git push
```
Then rotate every API key that was exposed — assume they are compromised the moment they appear in a public repo's git history.

**The scheduled run is at the wrong time**
Check the cron line in `.github/workflows/daily.yml`. The format is `minute hour * * *` in UTC.
6 am NZST (winter) = `0 18 * * *`. 6 am NZDT (summer) = `0 17 * * *`.

**Workflow runs but trades.csv is not updating**
The workflow commits via the `GITHUB_TOKEN`, which requires `permissions: contents: write` in the job.
Check that your `daily.yml` has that permission block exactly as shown — it is easy to accidentally delete on edits.
