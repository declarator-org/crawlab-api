# crawlab-api

Python client for the self-hosted [Crawlab](https://docs.crawlab.cn/) API.

Covers most of the API surface, read-heavy: **data collections, spiders,
schedules, tasks, results / logs, bulk export, projects, nodes, users, tags,
gits, tokens, settings, plugins, environments, stats**, plus a few
non-destructive actions (`run_spider`, `restart_task`, `enable_schedule`, …).
**No DELETE** is exposed, by design. The `tokens` and `gits` endpoints return
live secrets in cleartext — never log their output.

## Installation

Because the repository is private, you need a token or SSH access to GitHub.


### As a dependency in another `pyproject.toml`

```toml
[project]
dependencies = [
    "crawlab-api @ git+ssh://git@github.com/TI-Russia/crawlab_api.git@main",
]
```

Pin to a tag / commit for reproducibility:

```toml
"crawlab-api @ git+ssh://git@github.com/TI-Russia/crawlab_api.git@v0.6.3.1"
```

### With `uv add`

```bash
uv add "git+ssh://git@github.com/TI-Russia/crawlab_api.git@v0.6.3.1"
```

## Usage

### Working with data collections

```python
from crawlab_api import CrawlabClient

with CrawlabClient(base_url="https://crawlab.example.com", token="<token>") as client:
    # Create a data collection with deduplication
    # Crawlab will skip rows that have duplicate product_ids
    col = client.create_data_collection(
        name="products",
        fields=[
            {"name": "product_id", "type": "string"},
            {"name": "title", "type": "string"},
            {"name": "price", "type": "float"},
        ],
        dedup={
            "enabled": True,
            "keys": ["product_id"],        # deduplicate on this field
            "type": "ignore",              # skip if seen before
        },
    )
    
    # List all collections
    all_cols = client.list_data_collections(fetch_all=True)
    
    # Get a specific collection
    collection = client.get_data_collection(col["id"])
    
    # Update collection metadata (dedup config is required if enabled=True)
    updated = client.update_data_collection(col["id"], name="products_v2")
```

**Important:** If you enable deduplication (`enabled=True`), you **must** provide
at least one field name in `keys`. See [CLAUDE.md](./CLAUDE.md#deduplication-behavior)
for more details on deduplication strategies.

### Working with spiders and tasks

```python
from crawlab_api import CrawlabClient, Op

with CrawlabClient(base_url="https://crawlab.example.com", token="<token>") as client:
    # 1. find a spider
    spider = client.list_spiders(
        conditions=[{"key": "name", "op": Op.EQ, "value": "my-spider"}],
    )[0]

    # 2. list tasks of that spider, or of a schedule
    tasks = client.list_tasks(spider_id=spider["_id"], status="finished")
    sched_tasks = client.list_tasks(schedule_id="<schedule-id>", fetch_all=True)

    # 3. fetch per-task data (single task)
    for row in client.iter_task_data(tasks[0]["_id"]):
        print(row)

    # 3b. fetch all rows of the spider's collection, filtered by nd_id NOT IN [...]
    new_rows = client.iter_results_excluding(
        spider["col_id"],
        field="nd_id",
        exclude={123, 456, 789},
    )

    # logs
    for line in client.get_task_logs(tasks[0]["_id"]):
        print(line)
```

The `token` is the API token from Crawlab web UI (Settings → API Token);
it is sent as the raw `Authorization` header value, with no `Bearer` prefix.

### Data sources — read the store your results actually live in

`/results` returns a spider's rows, but the backend resolves the spider by
**both** `col_id` and `data_source_id`. If a spider writes to an **external**
data source (e.g. an external MongoDB) and you omit `data_source_id`, the
lookup fails with HTTP 500 `mongo: no documents in result`. Pass it — once, on
the client:

```python
from crawlab_api import CrawlabClient

with CrawlabClient(base_url="https://crawlab.example.com", token="<token>") as bootstrap:
    DS = next(d["_id"] for d in bootstrap.list_data_sources(fetch_all=True)
              if d["type"] == "mongo")           # discover the data source id

with CrawlabClient(base_url=..., token=..., data_source_id=DS) as client:
    rows = client.list_results(spider["col_id"], size=100)   # now works
```

`export` and `filter_field_options` (`/filters`) have **no** `data_source_id`
parameter — they read Crawlab's **built-in** MongoDB only. On instances that
write to an external store, those see a stale/partial copy, not the real data.
Use them only for built-in-store collections.

### Excluding already-downloaded rows (by `nd_id`)

Small exclude set — let the server filter, using the `$nin` hack (`/results`
needs `MongoOp.*`, see caveats):

```python
new_rows = client.iter_results_excluding(
    spider["col_id"], field="nd_id", exclude={123, 456, 789},
)   # data_source_id taken from the client
```

Large exclude set — `conditions` travels in the URL, so more than ~1–4k values
overflow it (HTTP 414 / `InvalidURL`). **Flip the direction**: pull the values
Crawlab already has and diff locally — nothing large is sent to the server:

```python
have = client.collection_field_values(spider["col_id"], "nd_id")  # via /results
to_download = my_candidate_ids - have                             # plain set difference
```

### Bulk export (built-in store only)

Export filters **server-side** and hands you a ready CSV / JSON file. It honours
the regular `Op.*` operators (so `Op.NIN` works) — the opposite of
`/results/{col_id}` (which needs `MongoOp.*`). Remember it reads the built-in
store only (see data-source note above).

```python
blob = client.export_collection_excluding(
    "json", "my_collection", field="nd_id", exclude={1, 2, 3},
)
rows = json.loads(blob)

# or drive the three steps yourself
export_id = client.create_export("csv", "my_collection")
status = client.get_export("csv", export_id)        # {"status": "finished", ...}
data = client.download_export("csv", export_id)     # bytes
```

> **Operator gotcha:** for export use the regular `Op.*`; passing `MongoOp.*`
> makes the export job fail server-side. This is the exact opposite of
> `/results/{col_id}`. See `CLAUDE.md` Bug E.

### Platform, stats and actions

```python
with CrawlabClient(base_url=..., token=...) as client:
    client.system_info()        # {"edition": ..., "version": "v0.6.3"}
    client.stats_overview()     # {"spiders": 8, "tasks": 33, "results": 10465, ...}
    client.get_current_user()   # the token owner

    projects = client.list_projects(fetch_all=True)
    nodes    = client.list_nodes()
    files    = client.list_spider_files(spider_id)        # source file tree

    # mutating actions (no DELETE is exposed anywhere)
    client.run_spider(spider_id, mode="random")
    client.restart_task(task_id)
    client.enable_schedule(schedule_id)
```

> ⚠ `list_tokens()` and `list_gits()` return live secrets (JWTs, git
> credentials) in cleartext. Never log or commit their output.

## Filtering (`conditions`)

List endpoints accept a `conditions=` keyword — a list of dicts
`{"key": ..., "op": ..., "value": ...}` joined with AND server-side. Operator
constants live in `crawlab_api.Op` (`EQ`, `NE`, `GT`, `IN`, `NIN`, `CONTAINS`,
`REGEX`, …). See `crawlab_api/conditions.py` for the full list.

Convenience shortcuts (`spider_id=`, `schedule_id=`, `status=`, `enabled=`) are
merged into `conditions` as `eq` comparisons.

**Backend caveats** — confirmed empirically against the Crawlab v0.6 backend:

- `conditions` works on `/spiders`, `/tasks`, `/schedules` and other generic
  list endpoints.
- `conditions` is **silently ignored** when `all=1` (i.e. `fetch_all=True`) is
  set together with a filter. This client works around the bug by transparently
  switching to client-side pagination when both are present.
- `/results/{col_id}` needs `data_source_id` when results live in an external
  data source, or it returns HTTP 500 `mongo: no documents in result`. Pass it
  per call or set it on the client. `export` / `filter_field_options` ignore
  `data_source_id` (built-in store only). See `CLAUDE.md` "Results & data
  sources".
- On `/results/{col_id}` (the spider's data collection endpoint), the
  backend op translator is broken. From the regular `Op.*` namespace only
  `EQ` works. **Use `MongoOp.*` instead** (`NIN`, `IN`, `NE`, `GT`, `GTE`,
  `LT`, `LTE`, `REGEX`) — these are the same operators with a `$` prefix
  and pass through the bug as valid Mongo expressions. The wrapper's
  `iter_results_excluding` uses `$nin` server-side by default; pass
  `client_side=True` to fall back to pulling all rows and filtering in
  Python.
- On `/export/{type}` the translator is the **correct** one, so the
  opposite rule holds: use the regular `Op.*` operators (`Op.NIN` works);
  `MongoOp.*` makes the export job fail. `target` is the collection name,
  not its id.

## Available methods

| Method | Description |
| --- | --- |
| `list_spiders` / `iter_spiders` | Spiders, paginated / streamed |
| `get_spider(spider_id)` | Single spider |
| `list_schedules` / `iter_schedules` | Schedules, with `spider_id` / `enabled` shortcuts |
| `get_schedule(schedule_id)` | Single schedule |
| `list_tasks` / `iter_tasks` | Tasks, with `spider_id` / `schedule_id` / `status` shortcuts |
| `get_task(task_id)` | Single task |
| `get_task_data(task_id, ...)` / `iter_task_data(task_id, ...)` | Rows produced by one task |
| `get_task_logs(task_id, ...)` | Log lines for a task |
| `list_results(col_id, ...)` / `iter_results(col_id, ...)` | All rows of a collection (pass `data_source_id` for external stores) |
| `iter_results_excluding(col_id, field=..., exclude={...})` | Server-side `$nin` over `/results` (`client_side=True` filters in Python) |
| `collection_field_values(col_id, field="nd_id", ...)` | Distinct field values via `/results` (the real store) |
| `filter_field_options(col_name, field="nd_id", ...)` | Server-side distinct via `/filters` (built-in store only) |
| `list_data_sources` / `get_data_source` | Configured external data sources |
| `list_data_collections` / `iter_data_collections` / `get_data_collection` | Registered data collections |
| `create_data_collection` / `update_data_collection` | Create / update a collection (with dedup config) |
| `create_export` / `get_export` / `download_export` | Low-level export: start / status / download |
| `export_collection(type, name, conditions=...)` | Start → poll → download, returns file bytes |
| `export_collection_excluding(type, name, field=..., exclude={...})` | Export everything except matching rows (server-side `Op.NIN`) |
| `list_projects` / `iter_projects` / `get_project` | Projects |
| `list_nodes` / `iter_nodes` / `get_node` | Nodes |
| `list_users` / `iter_users` / `get_user` / `get_current_user` | Users (+ token owner) |
| `list_tags` / `iter_tags` | Tags |
| `list_gits` / `iter_gits` / `get_git` ⚠ | Git repos (cleartext credentials) |
| `list_tokens` ⚠ | API tokens (cleartext JWTs) |
| `list_settings` / `get_setting` | Server settings |
| `list_plugins` / `get_plugin` | Plugins |
| `list_roles` / `list_permissions` | RBAC roles / permissions |
| `list_environments` / `get_environment` | Shared env variables |
| `list_notification_settings` | Notification settings |
| `system_info` / `stats_overview` / `stats_daily` | Version, dashboard totals, per-day counts |
| `list_spider_files` / `get_spider_file` / `get_spider_file_info` | Spider source file tree / contents |
| `run_spider(spider_id, ...)` | Trigger a run (creates tasks) |
| `restart_task` / `cancel_task` | Re-run / cancel a task |
| `enable_schedule` / `disable_schedule` | Toggle a schedule's cron trigger |

**No DELETE methods are exposed**, by design.















