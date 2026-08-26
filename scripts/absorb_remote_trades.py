"""Absorb any trade rows added to origin/main by a concurrent scan or monitor
run, merging them into the local trade CSVs before they are committed.

Must run before `git commit` in the CI workflows so that the later
`git pull --rebase -X ours origin main` preserves these rows (it picks our
version, which by then already includes them).

2026-08-26: generalized from trades.csv-only to also cover
research_trades.csv, which had no equivalent protection in any workflow —
confirmed via audit that intraday.yml also lacked even the trades.csv-only
version of this script entirely.
"""
import csv
import io
import subprocess
import sys

FILES = ("data/trades.csv", "data/research_trades.csv")


def _absorb_file(path: str) -> None:
    result = subprocess.run(["git", "show", f"origin/main:{path}"],
                             capture_output=True)
    if result.returncode != 0:
        return

    with open(path, encoding="utf-8-sig") as f:
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
    print(f"Pre-merge: absorbing {len(new_rows)} new row(s) into {path} from origin/main", flush=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(all_rows)
    subprocess.run(["git", "add", path], check=False)


def main() -> None:
    subprocess.run(["git", "fetch", "origin", "main", "--quiet"],
                    check=False, capture_output=True)
    for path in FILES:
        try:
            _absorb_file(path)
        except Exception as e:
            print(f"Pre-merge skipped for {path} (non-fatal): {e}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Pre-merge skipped (non-fatal): {e}", file=sys.stderr)
