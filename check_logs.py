"""Fetch GitHub Actions logs for daily.yml runs to diagnose Telegram issue."""
import urllib.request
import json
import os
import sys
from dotenv import load_dotenv

load_dotenv(".env")
pat = os.getenv("GIT_HUB", "")
if not pat:
    sys.exit("No GIT_HUB in .env")

HEADERS = {
    "Authorization": f"Bearer {pat}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

def gh(path):
    req = urllib.request.Request(f"https://api.github.com{path}", headers=HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

# 6am run = 27225905679, 5pm run = 27254514883
for label, run_id in [("6am scan (no Telegram)", 27225905679), ("5pm scan (working)", 27254514883)]:
    print(f"\n{'='*60}")
    print(f"RUN: {label}  |  run_id={run_id}")
    print("="*60)
    data = gh(f"/repos/bodyheath/forex-ai/actions/runs/{run_id}/jobs")
    for j in data.get("jobs", []):
        print(f"\nJob: {j['name']}  [{j['status']}] [{j['conclusion']}]")
        job_id = j["id"]
        for s in j.get("steps", []):
            print(f"  {s['number']:>2}. {s['name']:<40} {s['status']:<12} {s['conclusion']}")
        # get the log for this job
        try:
            log_req = urllib.request.Request(
                f"https://api.github.com/repos/bodyheath/forex-ai/actions/jobs/{job_id}/logs",
                headers=HEADERS,
            )
            log_bytes = urllib.request.urlopen(log_req, timeout=30).read()
            log_text = log_bytes.decode("utf-8", errors="replace")
            # Print lines mentioning Telegram or errors
            relevant = [ln for ln in log_text.splitlines()
                        if any(kw in ln for kw in ("TELEGRAM", "telegram", "ERROR", "error", "Error", "Traceback", "Exception", "FAILED", "guard", "scan_mode", "Sending", "[full]", "[asian]", "[midday]", "[prelondon]"))]
            if relevant:
                print("\n  --- Relevant log lines ---")
                for ln in relevant[:80]:
                    print(f"  {ln}")
            else:
                print("\n  (no relevant log lines found)")
        except Exception as exc:
            print(f"  Could not fetch logs: {exc}")
