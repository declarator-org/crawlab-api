"""Dump data of every task that belongs to a given schedule.

Run:
    uv run python scripts/dump_schedule_tasks.py [schedule_id]

If schedule_id is omitted, dumps tasks of every schedule on the server.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from crawlab_api import CrawlabClient, CrawlabError


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def dump_schedule(client: CrawlabClient, schedule_id: str) -> None:
    schedule = client.get_schedule(schedule_id)
    spider_id = schedule.get("spider_id")
    spider_name = None
    if spider_id:
        try:
            spider_name = client.get_spider(spider_id).get("name")
        except CrawlabError:
            spider_name = None

    print(f"# schedule {schedule_id}")
    print(f"#   name:     {schedule.get('name')!r}")
    print(f"#   cron:     {schedule.get('cron')!r}")
    print(f"#   enabled:  {schedule.get('enabled')}")
    print(f"#   spider:   {spider_id}  ({spider_name!r})")

    tasks = client.list_tasks(schedule_id=schedule_id, fetch_all=True)
    print(f"#   tasks:    {len(tasks)}\n")

    for idx, task in enumerate(tasks, 1):
        task_id = task.get("_id")
        status = task.get("status")
        created = task.get("create_ts")
        print(f"{'=' * 72}")
        print(
            f"task {idx}/{len(tasks)}  id={task_id}  status={status!r}  created={created}"
        )
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


def main(argv: list[str]) -> int:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    base_url = os.environ.get("CRAWLAB_BASE_URL")
    token = os.environ.get("CRAWLAB_TOKEN")
    if not base_url or not token:
        print("ERROR: set CRAWLAB_BASE_URL and CRAWLAB_TOKEN (env or .env)")
        return 2

    try:
        with CrawlabClient(base_url=base_url, token=token) as client:
            if len(argv) > 1:
                schedule_ids = [argv[1]]
            else:
                schedules = client.list_schedules(fetch_all=True)
                schedule_ids = [s["_id"] for s in schedules]
                if not schedule_ids:
                    print("no schedules on the server")
                    return 0
                print(f"# found {len(schedule_ids)} schedule(s)\n")

            for schedule_id in schedule_ids:
                dump_schedule(client, schedule_id)
                print()

    except CrawlabError as exc:
        print(f"\nCrawlabError: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
