"""Диагностика: что происходит со спайдером при смене коллекции результатов.

Только ЧТЕНИЕ. Сопоставляет spider.col_id с зарегистрированными
data collections и ищет "осиротевшие" коллекции — на которые не ссылается
ни один спайдер (типичный след прошлой коллекции спайдера, у которого
col_id переключили на другую).

Run:
    uv run python scripts/diag_collection_switch.py
"""

from __future__ import annotations

import os
from collections import Counter
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


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    base_url = os.environ["CRAWLAB_BASE_URL"]
    token = os.environ["CRAWLAB_TOKEN"]
    client = CrawlabClient(base_url=base_url, token=token)

    spiders = list(client.iter_spiders())
    cols = list(client.iter_data_collections())

    print(f"spiders={len(spiders)}  data_collections={len(cols)}\n")

    col_by_id = {c.get("_id") or c.get("id"): c for c in cols}

    # col_id -> сколько спайдеров на него ссылаются
    refs = Counter()
    print("=== СПАЙДЕРЫ (name | col_id | col_name | data_source_id) ===")
    for s in spiders:
        cid = s.get("col_id")
        refs[cid] += 1
        cobj = col_by_id.get(cid)
        cname_meta = cobj.get("name") if cobj else "<НЕТ такой коллекции в метаданных>"
        print(f"  {s.get('name'):35.35} | {cid} | col_name={s.get('col_name')!r:20} "
              f"| ds={s.get('data_source_id')} | meta_name={cname_meta!r}")

    # коллекции, на которые НЕ ссылается ни один спайдер
    referenced = {cid for cid in refs if cid}
    orphans = [c for c in cols if (c.get("_id") or c.get("id")) not in referenced]
    print(f"\n=== ОСИРОТЕВШИЕ КОЛЛЕКЦИИ (не привязаны ни к одному спайдеру): {len(orphans)} ===")
    for c in orphans:
        print(f"  id={c.get('_id') or c.get('id')}  name={c.get('name')!r}")

    # спайдеры, делящие один col_id
    shared = {cid: n for cid, n in refs.items() if cid and n > 1}
    if shared:
        print(f"\n=== col_id, общий для нескольких спайдеров: {shared} ===")

    # эмпирически: дёрнем /results на осиротевшую коллекцию — ожидаем 500
    if orphans:
        oc = orphans[0]
        ocid = oc.get("_id") or oc.get("id")
        print(f"\n=== ПРОБА /results/{ocid} (осиротевшая '{oc.get('name')}') ===")
        for label, kwargs in [("без data_source_id", {}),
                              ("с data_source_id спайдера", {})]:
            ds = next((s.get("data_source_id") for s in spiders if s.get("data_source_id")), None)
            if label.startswith("с") and ds:
                kwargs = {"data_source_id": ds}
            elif label.startswith("с"):
                continue
            try:
                rows = client.list_results(ocid, page=1, size=1, **kwargs)
                print(f"  {label}: OK, rows={len(rows)}")
            except CrawlabError as e:
                print(f"  {label}: ОШИБКА -> {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
