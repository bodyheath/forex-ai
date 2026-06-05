# External Scheduler Setup: cron-job.org → GitHub Actions

cron-job.org fires an HTTP POST to the GitHub API to trigger the workflow.
The existing GitHub Actions cron schedule remains active as a fallback.

---

## Step 1 — Create a GitHub Personal Access Token

1. Go to **github.com** and sign in
2. Click your **profile picture** (top-right) → **Settings**
3. Scroll to the bottom of the left sidebar → **Developer settings**
4. **Personal access tokens** → **Tokens (classic)**
5. Click **Generate new token** → **Generate new token (classic)**
6. **Note:** `forex-ai-scheduler`
7. **Expiration:** `No expiration`
8. **Scopes:** tick **`workflow`** only — nothing else is needed
9. Click **Generate token**
10. **Copy the token immediately** — it is shown only once

---

## Step 2 — Set Up cron-job.org

Go to **cron-job.org**, create a free account, then create **two cronjobs**
(one for Auckland winter, one for Auckland summer / daylight saving).

### Cronjob 1 — Auckland Winter (NZST, UTC+12): triggers at 6:05 am

| Field | Value |
|---|---|
| Title | `forex-ai winter (NZST)` |
| URL | `https://api.github.com/repos/bodyheath/forex-ai/actions/workflows/daily.yml/dispatches` |
| Request method | `POST` |
| Schedule → Hours | `18` |
| Schedule → Minutes | `5` |
| Schedule → Days of week | `Every day` |

**Request headers** (add each individually under the Headers tab):

| Header name | Header value |
|---|---|
| `Authorization` | `Bearer YOUR_GITHUB_PAT` |
| `Accept` | `application/vnd.github+json` |
| `Content-Type` | `application/json` |
| `X-GitHub-Api-Version` | `2022-11-28` |

Replace `YOUR_GITHUB_PAT` with the token from Step 1.

**Request body:**
```json
{"ref": "main"}
```

---

### Cronjob 2 — Auckland Summer / Daylight Saving (NZDT, UTC+13): triggers at 6:05 am

Identical to Cronjob 1 except:

| Field | Value |
|---|---|
| Title | `forex-ai summer (NZDT)` |
| Schedule → Hours | `17` |
| Schedule → Minutes | `5` |

All headers and body are the same.

---

## Summary: exact values to copy-paste

**URL**
```
https://api.github.com/repos/bodyheath/forex-ai/actions/workflows/daily.yml/dispatches
```

**Method**
```
POST
```

**Headers**
```
Authorization: Bearer YOUR_GITHUB_PAT
Accept: application/vnd.github+json
Content-Type: application/json
X-GitHub-Api-Version: 2022-11-28
```

**Body**
```json
{"ref": "main"}
```

**Cron schedules**
```
05 18 * * *   ← Auckland winter  (NZST UTC+12, Apr–Oct)
05 17 * * *   ← Auckland summer  (NZDT UTC+13, Oct–Apr)
```

---

## Testing

After saving each cronjob on cron-job.org you can click **Run now** to fire it
immediately. Then check:
**github.com/bodyheath/forex-ai/actions** — a new *Daily Forex Analysis* run
triggered by a `workflow_dispatch` event should appear within seconds.

You can also test locally:
```bash
GITHUB_PAT=your_token_here python trigger_workflow.py
```

---

## Fallback

The GitHub Actions schedule (`5 17 * * 1-5` and `5 18 * * 1-5`) remains in
`daily.yml` as a backup. If cron-job.org misses a trigger, GitHub will still
fire the workflow on weekdays. The two sources may occasionally both fire on
the same day — GitHub Actions deduplicates runs gracefully.
