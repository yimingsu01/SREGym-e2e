from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path

from distributed.remote import RemoteNode

logger = logging.getLogger(__name__)


async def collect_all(nodes: list[RemoteNode], run_id: str, local_base: Path | None = None) -> Path:
    """SCP results from all nodes and merge into a single CSV.

    Returns the path to the merged CSV.
    """
    base = local_base or Path("results") / f"distributed_{run_id}"
    base.mkdir(parents=True, exist_ok=True)

    tasks = [node.collect_results(base) for node in nodes]
    await asyncio.gather(*tasks, return_exceptions=True)

    merged_path = merge_csv_files(base)
    return merged_path


def merge_csv_files(base_dir: Path) -> Path:
    """Find all *_ALL_results.csv files under base_dir and merge them."""
    all_rows: list[dict] = []

    for csv_file in base_dir.rglob("*_ALL_results.csv"):
        try:
            with open(csv_file, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    all_rows.append(row)
        except Exception as e:
            logger.error(f"Failed to read {csv_file}: {e}")

    merged_path = base_dir / "ALL_results_merged.csv"
    if all_rows:
        fieldnames = sorted({key for row in all_rows for key in row})
        with open(merged_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        logger.info(f"Merged {len(all_rows)} results into {merged_path}")
    else:
        logger.warning("No result CSVs found to merge")
        merged_path.touch()

    return merged_path
