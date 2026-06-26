"""Smoke test against a live Crawlab instance.

Loads CRAWLAB_BASE_URL and CRAWLAB_TOKEN from a sibling .env file (or env vars),
walks the spider -> tasks -> task data path, and prints a brief summary.

Run:
    uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from crawlab_api import CrawlabClient, CrawlabError


def load_dotenv(path: Path) -> None:
    """Tiny KEY=VALUE parser — no python-dotenv dependency."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def mask(token: str) -> str:
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}…{token[-4:]} (len={len(token)})"


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    base_url = os.environ.get("CRAWLAB_BASE_URL")
    token = os.environ.get("CRAWLAB_TOKEN")
    if not base_url or not token:
        print("ERROR: set CRAWLAB_BASE_URL and CRAWLAB_TOKEN (env or .env)")
        return 2

    print(f"base_url: {base_url}")
    print(f"token:    {mask(token)}")
    print()

    try:
        with CrawlabClient(base_url=base_url, token=token) as client:
            # --- spiders ---
            print("=== spiders ===")
            spiders = client.list_spiders(size=5)
            print(f"got {len(spiders)} spiders (first page, size=5)")
            for s in spiders:
                print(f"  - {s.get('_id')}  name={s.get('name')!r}")
            if not spiders:
                print("no spiders — stopping")
                return 0

            spider = spiders[0]
            spider_id = spider.get("_id")
            print(f"\npicked spider: {spider_id} ({spider.get('name')!r})")

            # --- tasks ---
            print("\n=== tasks ===")
            tasks = client.list_tasks(spider_id=spider_id, size=5)
            print(f"got {len(tasks)} tasks for spider {spider_id}")
            for t in tasks:
                print(
                    f"  - {t.get('_id')}  status={t.get('status')!r}  "
                    f"created_ts={t.get('create_ts')}"
                )
            if not tasks:
                print("no tasks — stopping")
                return 0

            task = tasks[0]
            task_id = task.get("_id")
            print(f"\npicked task: {task_id}")

            # --- task data ---
            print("\n=== task data ===")
            data = client.get_task_data(task_id, size=3)
            print(f"got {len(data)} data rows (first page, size=3)")
            for row in data:
                preview = {k: row[k] for k in list(row)[:5]}
                print(f"  - {preview!r}")

            # --- task logs ---
            print("\n=== task logs ===")
            logs = client.get_task_logs(task_id, size=5)
            print(f"got {len(logs)} log lines (first 5)")
            for line in logs[:5]:
                print(f"  | {line[:200]}")

    except CrawlabError as exc:
        print(f"\nCrawlabError: {exc}")
        return 1

    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
