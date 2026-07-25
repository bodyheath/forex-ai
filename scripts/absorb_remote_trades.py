"""Absorb any trade rows added to origin/main by a concurrent scan or monitor
run, merging them into the local data/trades.csv before it is committed.

Must run before `git commit` in the CI workflows so that the later
`git pull --rebase -X ours origin main` preserves these rows (it picks our
version, which by then already includes them).
"""
import csv
import io
import subprocess
import sys


def main() -> None:
    subprocess.run(["git", "fetch", "origin", "main", "--quiet"],
                    check=False, capture_output=True)
    result = subprocess.run(["git", "show", "origin/main:data/trades.csv"],
                             capture_output=True)
    if result.returncode != 0:
        return

    with open("data/trades.csv", encoding="utf-8-sig") as f:
        local_content = f.read()
    local_reader = csv.DictReader(io.StringIO(local_content))
    fieldnames = local_reader.fieldnames
    if not fieldnames:
        return

    local_rows = list(local_reader)
    local_ids = {r["id"] for r in local_rows}
    remote_text = result.stdout.decode("utf-8-sig")
    new_rows = [r for r in csv.DictReader(io.StringIO(remote_text))
                if r["id"] not in local_ids]
    if not new_rows:
        return

    all_rows = sorted(local_rows + new_rows, key=lambda r: int(r.get("id") or 0))
    print(f"Pre-merge: absorbing {len(new_rows)} new row(s) from origin/main", flush=True)
    with open("data/trades.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(all_rows)
    subprocess.run(["git", "add", "data/trades.csv"], check=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Pre-merge skipped (non-fatal): {e}", file=sys.stderr)
