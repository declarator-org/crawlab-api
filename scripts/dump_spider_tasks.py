"""Dump data of every task for a given spider, one task at a time.

Run:
    uv run python scripts/dump_spider_tasks.py [spider_id]

If spider_id is omitted, defaults to 69fe15c8f08a8f9b6ed7f9e8.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from crawlab_api import CrawlabClient, CrawlabError

DEFAULT_SPIDER_ID = "69fe15c8f08a8f9b6ed7f9e8"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str]) -> int:
    spider_id = argv[1] if len(argv) > 1 else DEFAULT_SPIDER_ID

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    base_url = os.environ.get("CRAWLAB_BASE_URL")
    token = os.environ.get("CRAWLAB_TOKEN")
    if not base_url or not token:
        print("ERROR: set CRAWLAB_BASE_URL and CRAWLAB_TOKEN (env or .env)")
        return 2

    try:
        with CrawlabClient(base_url=base_url, token=token) as client:
            spider = client.get_spider(spider_id)
            print(f"# spider {spider_id}  name={spider.get('name')!r}")

            tasks = list(client.iter_tasks(spider_id=spider_id))
            print(f"# total tasks: {len(tasks)}\n")

            for idx, task in enumerate(tasks, 1):
                task_id = task.get("_id")
                status = task.get("status")
                print(f"{'=' * 72}")
                print(f"task {idx}/{len(tasks)}  id={task_id}  status={status!r}")
                print(f"{'=' * 72}")

                rows = list(client.iter_task_data(task_id))
                if not rows:
                    print("(no data)\n")
                    continue

                print(f"# rows: {len(rows)}")
                for row_idx, row in enumerate(rows, 1):
                    print(f"--- row {row_idx} ---")
                    print(json.dumps(row, ensure_ascii=False, indent=2, default=str))
                print()

    except CrawlabError as exc:
        print(f"\nCrawlabError: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
