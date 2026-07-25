"""Create an integrity-checked online backup of feedback.db."""

from __future__ import annotations

import argparse
from pathlib import Path

from backend.backup import backup_database


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    print(backup_database(args.output_dir))


if __name__ == "__main__":
    main()
