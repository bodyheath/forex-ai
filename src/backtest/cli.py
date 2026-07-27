"""Manual entry point — never wired into any scheduled workflow.

Usage:
    python -m src.backtest.cli --hypothesis src/backtest/hypotheses/exclude_nzd.yaml
    python -m src.backtest.cli --list-datasets
"""

import argparse

from src.backtest import data_loader, engine, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hypothesis", help="Path to a hypothesis YAML file")
    parser.add_argument("--list-datasets", action="store_true",
                         help="List available named datasets and exit")
    args = parser.parse_args()

    if args.list_datasets:
        for name, desc in data_loader.list_datasets().items():
            print(f"{name}\n  {desc}\n")
        return

    if not args.hypothesis:
        parser.error("--hypothesis is required (or use --list-datasets)")

    result = engine.run_experiment(args.hypothesis)
    print(report.render_markdown(result))
    print(f"\nFull output written to: data/sandbox/experiments/{result['run_id']}/")


if __name__ == "__main__":
    main()
