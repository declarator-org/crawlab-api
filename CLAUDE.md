# crawlab-api — agent-facing notes

A Python wrapper for the **self-hosted** Crawlab REST API (`/api` mount).
Scope now covers nearly the whole API surface, read-heavy:

- **Data**: data collections, spiders, schedules, tasks, results / logs,
  spider source files, bulk export (CSV / JSON).
- **Platform read**: projects, nodes, users (+ `users/me`), tags, gits,
  tokens, settings, plugins, roles, permissions, environments,
  notification settings, `system-info`, `stats/overview`, `stats/daily`.
- **Actions** (mutating POST): `run_spider`, `restart_task`, `cancel_task`,
  `enable_schedule` / `disable_schedule`.

**DELETE is deliberately not implemented** (avoid accidental data loss).
The `tokens` and `gits` endpoints return live secrets (JWTs, git
credentials) in cleartext — never log or commit their output.

## Quick repo map

```
src/crawlab_api/
  client.py        CrawlabClient — sync httpx wrapper
  conditions.py    Op enum + `eq()` helper for the conditions DSL
  exceptions.py    CrawlabError hierarchy
scripts/
  smoke_test.py            spider -> task -> data sanity check
  dump_spider_tasks.py     dump per-task data for a given spider
  dump_schedule_tasks.py   dump per-task data for tasks created by a schedule
```

- Runtime dep: `httpx`. Tests would use `respx` (declared as `[dev]` extra).
- Build backend: `uv_build`, layout: `src/`.
- The `.env` file at repo root holds `CRAWLAB_BASE_URL` and `CRAWLAB_TOKEN`
  for live testing. Already in `.gitignore` — never commit it.

Crawlab backend source code (Go) is the source of truth for behaviour the
Swagger docs don't cover. When investigating new endpoints or fields,
sparse-clone `github.com/crawlab-team/crawlab` and grep
`core/controllers/`, `core/constants/`, `core/utils/`, `core/result/`.

## Authentication

The token is the raw value from Crawlab UI → Settings → API Token. Sent as
`Authorization: <token>` — no `Bearer` prefix. This is JWT but the server
expects it bare.

## How the data is structured server-side

```
Spider ──┬── col_id        (MongoDB collection holding all scraped rows)
         ├── tasks         (one Task per run; manual or schedule-triggered)
         │     ├── status, spider_id, schedule_id, create_ts, …
         │     ├── /tasks/{id}/data    rows produced by THIS task (joined by _tid)
         │     └── /tasks/{id}/logs    raw log lines from this run
         └── schedules     (cron-driven triggers; one schedule → many tasks)
               └── /schedules/{id}
```

- A row in the spider's collection has `_tid` = id of the task that wrote it.
- A task has `schedule_id` = id of the schedule that fired it, or
  `000000000000000000000000` (zero ObjectID) when the task was started manually.
- A schedule has `spider_id` pointing at the spider it runs.

## Filtering: the `conditions` DSL

Crawlab list endpoints accept a query parameter `conditions` — a JSON-encoded
array of `{"key": ..., "op": ..., "value": ...}` objects, AND-ed server-side.

Operator constants live in `crawlab_api.Op` and mirror
`core/constants/filter.go`: `EQ`, `NE`, `GT`, `GTE`, `LT`, `LTE`, `IN`, `NIN`,
`CONTAINS`, `NOT_CONTAINS`, `REGEX`, `SEARCH`, `NOT_SET`.

The backend auto-converts any string value that parses as a 24-char hex into
a MongoDB `ObjectID` (`core/controllers/utils_filter.go:26-60`). So passing
spider / task / schedule ids as plain strings just works.

`conditions` is NOT documented in the Crawlab Swagger UI — the UI only lists
`page` and `size`. Source of truth is the Go backend.

## Endpoints exposed by this wrapper

| Wrapper method | Server | Conditions accepted? |
|---|---|---|
| `list_data_collections` / `iter_data_collections` / `get_data_collection` | `/data/collections[/{id}]` | yes |
| `create_data_collection` | `POST /data/collections` | N/A |
| `update_data_collection` | `PUT /data/collections/{id}` | N/A |
| `list_spiders` / `iter_spiders` / `get_spider` | `/spiders[/{id}]` | yes |
| `list_schedules` / `iter_schedules` / `get_schedule` | `/schedules[/{id}]` | yes |
| `list_tasks` / `iter_tasks` / `get_task` | `/tasks[/{id}]` | yes |
| `get_task_data` / `iter_task_data` | `/tasks/{id}/data` | **NO — see Bug C** |
| `get_task_logs` | `/tasks/{id}/logs` | (not relevant) |
| `list_results` / `iter_results` / `iter_results_excluding` | `/results/{col_id}` | partially — see Bug B |
| `list_projects` / `iter_projects` / `get_project` | `/projects[/{id}]` | yes |
| `list_nodes` / `iter_nodes` / `get_node` | `/nodes[/{id}]` | yes |
| `list_users` / `iter_users` / `get_user` / `get_current_user` | `/users[/{id}]`, `/users/me` | yes |
| `list_tags` / `iter_tags` | `/tags` | yes |
| `list_gits` / `iter_gits` / `get_git` ⚠secrets | `/gits[/{id}]` | yes |
| `list_tokens` ⚠secrets | `/tokens` | yes |
| `list_settings` / `get_setting` | `/settings[/{id}]` | yes |
| `list_plugins` / `get_plugin` | `/plugins[/{id}]` | yes |
| `list_roles` / `list_permissions` | `/roles`, `/permissions` | yes |
| `list_environments` / `get_environment` | `/environments[/{id}]` | yes |
| `list_notification_settings` | `/notifications/settings` | yes |
| `system_info` | `/system-info` | N/A |
| `stats_overview` / `stats_daily` | `/stats/overview`, `/stats/daily` | N/A |
| `create_export` / `get_export` / `download_export` / `export_collection` / `export_collection_excluding` | `/export/{type}[/{id}[/download]]` | yes — **use `Op.*`, see Bug E** |
| `list_spider_files` / `get_spider_file` / `get_spider_file_info` | `/spiders/{id}/files/{list,get,info}` | N/A |
| `run_spider` | `POST /spiders/{id}/run` | N/A |
| `restart_task` / `cancel_task` | `POST /tasks/{id}/{restart,cancel}` | N/A |
| `enable_schedule` / `disable_schedule` | `POST /schedules/{id}/{enable,disable}` | N/A |

**Data collections** are MongoDB collections registered in Crawlab metadata,
used to store scraped results. The `spider.col_id` field points to the data
collection where the spider's results live. `list_results` is the cross-task
view of all rows. `get_task_data` is the per-task slice — both query the same
underlying MongoDB collection but with different query predicates.

| `collection_field_values(col_id, field)` | distinct field values via `/results` | yes (`MongoOp.*`) |
| `filter_field_options(col_name, field)` | server-side distinct via `/filters` (built-in store only) | yes (`Op.*`) |
| `list_data_sources` / `get_data_source` | `/data-sources[/{id}]` | yes |

> ⚠️ **`/results/{col_id}` requires `data_source_id` for external data sources.**
> It is NOT broken — `GetResultList` looks up the spider by
> `{col_id, data_source_id}` (defaulting the id to the zero ObjectID), so when
> results live in an external store you MUST pass `data_source_id` or it raises
> HTTP 500 `mongo: no documents in result`. See "Results & data sources" below.

## Backend bugs and gotchas (verified May–June 2026)

These are the non-obvious traps. The wrapper either works around them
transparently or warns in docstrings — please preserve those workarounds.

> **Source-of-truth caveat.** The reported version (`/system-info` → `v0.6.3`)
> is a STATIC string read from `backend/conf/config.yml`
> (`GetSystemInfo` → `viper.GetString("version")`); it does NOT identify the
> deployed commit. The Go backend (`crawlab-team/crawlab-core`, later merged
> inline under `core/`) kept shipping under "v0.6.3" for ~15 months. A local
> checkout at `main` / `v0.6.3-dev-119` is NEWER than the deployed release and
> matches prod on some behaviours (Bug E present) but not others (Bug A is
> fixed there — see below). When source and live behaviour disagree, trust the
> live probe and pin the deployed image to be certain.

### Bug A — `all=1` silently drops `conditions` (present on prod; fixed upstream)

The backend's generic list handler (`core/controllers/base_v2.go:GetList`)
splits into `getList(c)` (paginated) and `getAll(c)` (returns everything).
On the live instance, when `all=1` is set the response ignores any filter and
returns every row — verified against `/tasks`, `/spiders` and `/schedules`.

NOTE: in newer source (`base_v2.go:getAll`, 2024 monorepo) `getAll` DOES call
`MustGetFilterQuery` and applies the filter, so this is fixed in builds after
the deployed one. Keep the workaround while prod still exhibits it; re-verify
after a Crawlab upgrade.

Workaround (already implemented in `client._list`): if the caller sets
`fetch_all=True` AND any condition is present, switch to client-side
pagination and never send `all=1` to the server. `fetch_all=True` with no
filter still goes through the fast `all=1` path.

```python
# this is safe — conditions are honoured
tasks = client.list_tasks(schedule_id="...", fetch_all=True)
```

Empirical evidence (do not delete — useful to re-verify if Crawlab is upgraded):

```
schedule_id filter
  paginated  total=4 returned=4
  all=1      total=8 returned=8    ← bug
spider_id filter
  paginated  total=6 returned=6
  all=1      total=8 returned=8    ← bug
status filter
  paginated  total=1 returned=1
  all=1      total=8 returned=8    ← bug
```

### Bug B — `/results/{col_id}` has a broken op translator (with a usable hack)

The undocumented endpoint `GET /results/{col_id}` parses `conditions`
correctly into a `generic.ListQuery`, but then the Mongo translator
(`core/utils/mongo.go:GetMongoQuery`) special-cases only `eq`:

```go
switch c.Op {
case generic.OpEqual:
    res[c.Key] = c.Value
default:
    res[c.Key] = bson.M{
        c.Op: c.Value,        // ← missing $ prefix!
    }
}
```

So sending `op="nin"` produces `bson.M{"nin": [...]}` which MongoDB does
NOT recognise — zero matches, no error.

**The hack:** pass the operator already `$`-prefixed (`op="$nin"`). The
default branch then produces `bson.M{"$nin": [...]}`, a valid Mongo
expression. The wrapper exposes these as `crawlab_api.MongoOp.{NIN, IN, NE,
GT, GTE, LT, LTE, REGEX}` — use these on `/results/{col_id}` instead of
the regular `Op.*` names.

Empirical evidence (against a collection of 6 rows, 4 unique `nd_id` values):

```
baseline (no filter)              total=6   unique_nd_ids=[303, 590, 075, 076]
eq nd_id=076                      total=2   ✓
in nd_id [076]                    total=0   ← Op.IN, broken
nin nd_id [076]                   total=0   ← Op.NIN, broken
"$in" nd_id [076,075]             total=4   ✓ MongoOp.IN
"$nin" nd_id [076]                total=4   ✓ MongoOp.NIN
"$nin" nd_id [076, 075]           total=2   ✓
"$gt" nd_id 610109300             total=6   ✓
"$ne" nd_id 076                   total=4   ✓
```

This hack is bug-driven: if Crawlab ever fixes `GetMongoQuery` to auto-prefix
`$`, our `$nin` will become `$$nin` and stop matching. Because of that,
`iter_results_excluding` keeps a client-side fallback path: pass
`client_side=True` to iterate everything and filter in Python.

For the common "rows whose `nd_id` is NOT in [list]" workflow, just call
`iter_results_excluding(col_id, field=..., exclude={...})` — by default it
uses the `$nin` hack, which is dramatically faster than the client-side
pull-and-filter approach, especially when the collection is large and only
a small fraction is excluded.

If the exclude set is large enough to bump against MongoDB's 16 MB
per-query limit, switch to `client_side=True` or query in batches.

### Bug C — `/tasks/{id}/data` ignores `conditions` entirely

Unlike `/results/{col_id}`, the per-task endpoint
`GET /tasks/{id}/data` does NOT call `GetFilter` at all. Its query is
hard-coded to `{_tid: task_id}` (`core/controllers/task_v2.go:GetTaskData`,
lines 463-469):

```go
query := generic.ListQuery{
    generic.ListQueryCondition{
        Key:   constants.TaskKey,   // "_tid"
        Op:    generic.OpEqual,
        Value: t.Id,
    },
}
```

So `conditions` passed to `/tasks/{id}/data` are silently dropped. The
wrapper does NOT expose a `conditions` parameter on `get_task_data` to
avoid signalling that filtering works.

To filter task rows by an arbitrary field, go through
`list_results(spider.col_id, ...)` instead — it filters across all tasks
(remember Bug B for op support) and rows include `_tid` so you can
re-associate them to tasks client-side.

### Results & data sources — `/results/{col_id}` needs `data_source_id` (NOT a bug)

`GET /results/{col_id}` returns HTTP 500 `mongo: no documents in result`
when called without `data_source_id` **if the spider's results live in an
external data source**. This is a parameter requirement, not a breakage — the
fix is to pass the right `data_source_id`. Root cause in the source
(`core/controllers/result_v2.go:GetResultList`, confirmed against the live
backend):

```go
dcId, _ := primitive.ObjectIDFromHex(c.Param("id"))   // :id IS the col_id
dc, _  := GetById[DataCollectionV2](dcId)
ds, _  := GetById[DatabaseV2](dsId)                   // dsId from ?data_source_id, else ZERO ObjectID
s, err := GetOne[SpiderV2](bson.M{
    "col_id":         dc.Id,
    "data_source_id": ds.Id,                           // ← must match a real spider
})
if err != nil { HandleErrorInternalServerError(c, err); return }  // ErrNoDocuments → 500
svc, _ := result.GetResultService(s.Id)                // queries the spider's data source
```

So the spider lookup is keyed on BOTH `col_id` and `data_source_id`. If the
spiders carry a real `data_source_id` (an external Mongo/PG store) and you
omit it, the query `{col_id, data_source_id: 0}` matches nothing → 500.

Verified live (June 2026) — the production instance stores results in an
external Mongo data source:

```
GET /results/69fb819bc99a0ed705b4f5ce                                  -> 500 "no documents"
GET /results/69fb819bc99a0ed705b4f5ce?data_source_id=69fb7d82…b4f5c9   -> total=5644  ✓
```

So the flagship `list_results` / `iter_results` / `iter_results_excluding`
work fine — pass `data_source_id`, or set it once on the client:

```python
DS = next(d["_id"] for d in client.list_data_sources(fetch_all=True)
          if d["type"] == "mongo")
client = CrawlabClient(base_url=..., token=..., data_source_id=DS)
have = client.collection_field_values(spider["col_id"], "nd_id")   # reads the REAL store
to_fetch = candidate_ids - have
```

**Important store split (verified):** `export` (`PostExport`) and `/filters`
(`GetFilterColFieldOptions`) have NO `data_source_id` parameter — they read
Crawlab's BUILT-IN MongoDB only. On instances that write to an external data
source, those endpoints see a stale/partial copy (e.g. 6 rows) rather than the
real results (e.g. 5644). For external-store data, read via `/results`
(`list_results` & friends, with `data_source_id`); reserve `export` /
`filter_field_options` for built-in-store collections.

### Bug E — `/export/{type}` and `/results/{col_id}` use OPPOSITE operator namespaces

Both endpoints filter the same underlying Mongo collections, but through
**different translators**, so the operator namespace flips:

| Endpoint | Translator | Use | Broken |
|---|---|---|---|
| `/results/{col_id}` | `core/utils/mongo.go:GetMongoQuery` (no `$` prefix) | only `Op.EQ`; for the rest use **`MongoOp.*`** (`$nin`, `$in`…) | `Op.NIN/IN/GT` → silently 0 rows |
| `/export/{type}` | proper generic filter→Mongo | the regular **`Op.*`** (`Op.NIN`, `Op.IN`, `Op.GT` all work) | `MongoOp.*` → export job **fails** (status="error") |

Verified June 2026 against a 6-row collection (`nd_id` is an **int**):

```
export Op.EQ  nd_id=610163076       -> 2 rows  ✓
export Op.NIN nd_id [610163076]     -> 4 rows  ✓   (results endpoint needs $nin here)
export Op.IN  nd_id [610163076]     -> 2 rows  ✓
export Op.GT  nd_id 610163075       -> 2 rows  ✓
export MongoOp.NIN ($nin) …         -> status="error"  ✗
```

Practical upshot: to pull "everything not yet downloaded" prefer
**`export_collection_excluding(..., field="nd_id", exclude={...})`** — it
filters server-side with `Op.NIN` (which export supports correctly) and
returns a ready CSV/JSON file. `iter_results_excluding` does the same over
`/results` but must use the `$nin` hack. `export_collection` raises
`CrawlabAPIError` immediately if the job reports `status="error"` instead
of waiting for the timeout.

Also note: `target` for export is the collection **name**, not its id.

**URL-length cap (verified June 2026).** `conditions` travels in the query
string for both export and `/results`. nginx in front of Crawlab returns
**HTTP 414** once the query exceeds ~8 KB, and httpx itself raises
`InvalidURL: URL component 'query' too long` past ~60 KB. Concretely, an
`Op.NIN` / `Op.IN` list overflows at roughly **1 000–4 000 integer values**
(fewer for longer string values):

```
nin nd_id [1000 ints]  url≈4.9KB  -> 200 OK
nin nd_id [5000 ints]  url≈29KB   -> 414 Request-URI Too Large
nin nd_id [10000 ints] url≈59KB   -> httpx InvalidURL (never sent)
```

So `export_collection_excluding` / `iter_results_excluding` with a large
`exclude` set fail. **Flip the direction instead**: pull the existing values
with `collection_field_values(...)` and compute the difference in Python —
nothing large is ever sent to the server. Export cannot batch an "exclude"
across requests (each partial file would still contain rows excluded only by
the other batches), so client-side diffing is the correct pattern.

### Bug D — `filter=` is silently ignored (not a real parameter)

Earlier drafts of this wrapper used `filter={"key": value}` as the filter
syntax — because that's what other APIs commonly use, and the conventional
Swagger docs suggested it. Crawlab does NOT have a `filter` query param.
Sending it produces no error and no effect; the server returns every row.

This is what cost us the most time to find. If something is "not filtering",
first sanity-check by sending an obviously-impossible value (`bogus_id`) —
if `total` doesn't drop to 0, the filter is being ignored.

## Patterns the wrapper uses

- All `list_*` methods route through `client._list`, which centralises
  pagination, `all=1`, and the Bug A workaround.
- All `iter_*` methods use `_paginate(fetch_page_callable, size)` — they
  stop when a page is short, never make a wasted last request.
- Response unwrapping happens in `_handle`: Crawlab wraps responses as
  `{"data": ..., "total": N, "message": "..."}`; the wrapper returns `data`
  directly. Listing endpoints return a list, single-object endpoints return
  a dict.
- HTTP error mapping: 401/403 → `CrawlabAuthError`, 404 →
  `CrawlabNotFoundError`, otherwise `CrawlabAPIError(status, message, payload)`.
  Catch the base `CrawlabError` for "anything went wrong".

## Using data collections

Data collections are independent from spiders — they're registered in Crawlab
metadata so the UI and API can track the structure of your scraped data. A
spider points to a collection via `spider.col_id`.

**Create a collection before assigning to a spider:**

```python
# Create a collection with optional field schema and dedup config
col = client.create_data_collection(
    name="my_products",
    fields=[
        {"name": "product_id", "type": "string"},
        {"name": "title", "type": "string"},
        {"name": "price", "type": "float"},
    ],
    dedup={
        "enabled": True,
        "keys": ["product_id"],
        "type": "deduplicate",
    },
)
col_id = col["id"]  # use this as spider.col_id
```

**Update a collection:**

```python
# Update just the name, leave other fields unchanged
updated = client.update_data_collection(
    col_id,
    name="updated_products",
)
```

**Query all collections:**

```python
# List all collections
all_cols = client.list_data_collections(fetch_all=True)

# Filter by name
matching = client.list_data_collections(
    conditions=[{"key": "name", "op": Op.CONTAINS, "value": "product"}]
)

# Iterate over collections
for col in client.iter_data_collections():
    print(f"Collection: {col['name']} (id={col['id']})")
```

### Switching a spider's collection orphans the old data (verified June 2026)

A spider holds **one** `col_id`. "Pointing the spider at a different collection"
just overwrites `col_id` — the previous value is kept nowhere on the spider, so
the link to the old collection is severed instantly.

Because `GET /results/{col_id}` does **not** read the collection directly — it
first reverse-looks-up the spider via `GetOne[SpiderV2]({col_id, data_source_id})`
and only then reads through that spider's result service (see "Results & data
sources" above) — data availability hinges on **a spider still referencing the
collection**, not on the collection itself.

So after switching a spider from collection A to collection B:

| | Result |
|---|---|
| New tasks | write to B (`spider.col_id=B`) ✓ |
| `list_results(spider.col_id)` | returns **only** B's rows; A is invisible |
| `/results/{A}` directly (even with the right `col_id`) | **HTTP 500 `mongo: no documents in result`** if no spider references A — the `{col_id:A, data_source_id}` lookup matches nothing |
| `/tasks/{old_task_id}/data` | **empty** — the spider's result service now points at B, but the old task's `_tid` rows live in A |
| The data itself | **NOT deleted** — still sits in Mongo collection A, reachable only by direct Mongo access (or, for built-in store, `export` by collection **name**) |

This is a silent loss of API access to prior results, with no data deletion.
Conversely, several spiders **may share one `col_id`** (verified: 3 spiders on
`pravo_gov_assignments_government`) — then the reverse lookup resolves to *some*
spider and all of them read/write one physical collection, rows told apart only
by `_tid`.

**Diagnosing it:** an "orphaned" data collection — registered in metadata but
referenced by no spider's `col_id` — is the tell-tale fingerprint of a past
switch. `scripts/diag_collection_switch.py` lists spiders vs collections, flags
orphans and shared `col_id`s, and probes `/results/{orphan}` (read-only).

If old results must stay API-readable, don't repoint `col_id`: separate runs by
`_tid`/fields within one collection, keep a dedicated spider on the old
collection, or pull the old rows straight from Mongo by collection name.

## Deduplication behavior

Data collections support deduplication to prevent duplicate rows in scraped
results. The deduplication is performed by Crawlab when inserting results
(see `core/result/service_mongo.go:43` in the Crawlab backend source).

### How it works

When `dedup.enabled=True`, Crawlab:
1. Computes an MD5 hash of the specified field values
2. Stores the hash in a reserved `_h` field
3. Before inserting a row, checks if the hash already exists
4. Applies one of two strategies:
   - **"ignore"** (default): skips the row if hash matches
   - **"overwrite"**: updates the existing row if hash matches, otherwise inserts

### Configuration

To enable deduplication, provide a `dedup` dict with:

```python
dedup={
    "enabled": True,
    "keys": ["field1", "field2"],  # Required! Cannot be empty if enabled=True
    "type": "ignore",               # or "overwrite"
}
```

The `keys` field **must not be empty** if `enabled=True`. The wrapper validates
this client-side and raises `ValueError` with a clear message if violated. This
prevents silent disabling of dedup on the server side.

### Examples

**Dedup by product ID (ignore duplicates):**

```python
col = client.create_data_collection(
    name="products",
    dedup={
        "enabled": True,
        "keys": ["product_id"],
        "type": "ignore",  # skip if seen before
    },
)
```

**Dedup by (seller, sku) pair (overwrite duplicates):**

```python
col = client.create_data_collection(
    name="seller_inventory",
    dedup={
        "enabled": True,
        "keys": ["seller_id", "sku"],
        "type": "overwrite",  # update price, quantity, etc on re-scrape
    },
)
```

**No deduplication:**

```python
col = client.create_data_collection(
    name="raw_logs",
    # omit dedup, or set dedup={"enabled": False}
)
```

### Important notes

- **Empty keys silently disable dedup on the server.** The wrapper catches this
  client-side, but if you bypass the wrapper and call the raw API, be careful.
- **Hash covers all values.** The hash is computed from the full set of `keys`
  values joined as JSON. Changing any key value results in a different hash.
- **Hashing is deterministic.** `{"a":1,"b":2}` always produces the same hash
  regardless of insertion order, but `{"b":2,"a":1}` also matches it.
- **Large dedup sets.** If deduplicating across millions of rows, the lookup
  is indexed on `_h` server-side, so performance is acceptable.

## Living conventions

- Sync-only client for now. If async is needed later, mirror the API on an
  `AsyncCrawlabClient` — don't try to support both via a magic switch.
- No pydantic — returns are plain `dict[str, Any]`. The backend's schema
  shifts slightly across Crawlab versions (e.g. `created_ts` vs `create_ts`),
  so typing it strictly would add maintenance cost with little payoff.
- `scripts/` is **not** part of the published package — only smoke tests
  and one-off dumps. Keep ad-hoc dotenv parsing inline; don't introduce a
  shared scripts helper module unless we have ≥3 scripts that need it.
- When verifying behaviour against the live server, ALWAYS print a `total`
  count alongside `len(data)`. They diverge only when something has gone
  silently wrong (e.g. Bug A) and that divergence is the cheapest signal.
- Data collection operations do NOT support deletion via this wrapper —
  if you need to delete a collection, do it via the Crawlab UI or a direct
  API call. This is intentional to prevent accidental data loss.

## When extending the wrapper

If you need to add an endpoint not currently exposed:

1. Find the Go handler under `core/controllers/`. Confirm it goes through
   the generic list controller or has its own implementation.
2. If it has its own implementation, check whether it calls `GetFilter`
   (then it supports `conditions`) and whether it calls `MustGetPagination`
   or `MustGetFilterAll`.
3. Test on the live server with both a real and a bogus filter value to
   verify filtering actually works — Crawlab tends to silently ignore
   filters rather than error.
4. If it touches the spider data collection (`/results/{col_id}` style),
   re-read Bug B before believing the operators work.

The wrapper now exposes most read endpoints (including `users`, `nodes`,
`tokens`, `plugins`, `gits`). What remains deliberately **out of scope**:

- **All DELETE operations** — never add destructive deletes without an
  explicit, narrowly-scoped request.
- `login` / token *minting* and other auth state changes — the wrapper
  authenticates with a pre-issued API token; it does not manage sessions.
- Write/PUT of platform config (settings, users, roles, git credentials).
  Reads are fine; mutating these is not in scope unless explicitly asked.

When adding the rare new mutating action, keep it under the "Actions"
section in `client.py` so side effects stay visually separated from reads.
