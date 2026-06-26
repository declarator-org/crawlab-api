"""Fetch a collection's rows, skipping nd_ids you already have — the real way.

Reads via /results (so it sees data stored in an EXTERNAL data source, unlike
export/`filters`), auto-discovering the data source id. Pass the already-have
nd_ids on stdin (one per line); the script flips the direction — it pulls the
nd_ids Crawlab has, diffs locally, and streams only the rows whose nd_id is new
(so a huge "have" set never goes into a URL).

Run:
    printf '610163076\\n610163075\\n' | \\
        uv run python scripts/fetch_new_results.py <col_id> [--field nd_id] [--ds <data_source_id>]
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


def parse_ids(text: str) -> set[object]:
    ids: set[object] = set()
    for raw in text.splitlines():
        token = raw.strip()
        if not token:
            continue
        ids.add(int(token) if token.lstrip("-").isdigit() else token)
    return ids


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1].startswith("-"):
        print(__doc__)
        return 2
    col_id = argv[1]
    field = argv[argv.index("--field") + 1] if "--field" in argv else "nd_id"
    ds = argv[argv.index("--ds") + 1] if "--ds" in argv else None

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    base_url = os.environ.get("CRAWLAB_BASE_URL")
    token = os.environ.get("CRAWLAB_TOKEN")
    if not base_url or not token:
        print("ERROR: set CRAWLAB_BASE_URL and CRAWLAB_TOKEN (env or .env)")
        return 2

    already_have = parse_ids(sys.stdin.read()) if not sys.stdin.isatty() else set()

    try:
        with CrawlabClient(base_url=base_url, token=token, data_source_id=ds) as client:
            # auto-discover an external Mongo data source if none was given
            if ds is None:
                sources = client.list_data_sources(fetch_all=True)
                mongo = next((s for s in sources if s.get("type") == "mongo"), None)
                if mongo is not None:
                    client._data_source_id = mongo["_id"]  # noqa: SLF001 (demo)
                    print(f"# using data source {mongo['_id']} ({mongo.get('name')})",
                          file=sys.stderr)

            count = 0
            for row in client.iter_results(col_id, size=500):
                if row.get(field) in already_have:
                    continue
                sys.stdout.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                count += 1
            print(f"# wrote {count} new rows (skipped {len(already_have)} known {field}s)",
                  file=sys.stderr)
    except CrawlabError as exc:
        print(f"\nCrawlabError: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
