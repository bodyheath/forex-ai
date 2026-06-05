"""
Manually trigger the Daily Forex Analysis GitHub Actions workflow.
Usage: GITHUB_PAT=<token> python trigger_workflow.py
"""

import os
import urllib.request
import urllib.error
import json

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
URL = "https://api.github.com/repos/bodyheath/forex-ai/actions/workflows/daily.yml/dispatches"

if not GITHUB_PAT:
    raise SystemExit("Error: GITHUB_PAT environment variable not set.")

payload = json.dumps({"ref": "main"}).encode()
headers = {
    "Authorization": f"Bearer {GITHUB_PAT}",
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
    "X-GitHub-Api-Version": "2022-11-28",
}

req = urllib.request.Request(URL, data=payload, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req) as resp:
        print(f"Triggered successfully: HTTP {resp.status}")
except urllib.error.HTTPError as e:
    raise SystemExit(f"Failed: HTTP {e.code} {e.reason} — {e.read().decode()}")
