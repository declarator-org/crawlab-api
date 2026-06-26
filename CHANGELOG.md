# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions track the Crawlab self-hosted backend release the wrapper was
verified against, with a trailing suffix for client-side iterations:
`<crawlab_version>.<client_iteration>`. So `0.6.3.1` is the first release
of this wrapper, verified against Crawlab `0.6.3`.

## [0.6.3.2] — 2026-06-10

Broad coverage expansion: the wrapper now reaches almost the entire Crawlab
`0.6.3` API surface (read-heavy, plus a few non-destructive actions).
**No DELETE operations are exposed**, by design. Verified live against the
self-hosted instance.

### Added
- Bulk export of data collections:
  - `create_export`, `get_export`, `download_export` (raw bytes).
  - `export_collection` — start → poll → download, raises `CrawlabAPIError`
    immediately on server-side `status="error"` instead of waiting out the
    timeout.
  - `export_collection_excluding(type, name, field=, exclude=)` — filters
    server-side with `Op.NIN` (small exclude sets only — see URL-length note).
    NOTE: export reads Crawlab's built-in store only (no `data_source_id`).
  - `EXPORT_CSV` / `EXPORT_JSON` constants.
- Data-source-aware results reads:
  - `data_source_id` on the client constructor (default for all `/results`
    reads) and the existing per-call `data_source_id=`. Required when results
    live in an external data source.
  - `list_data_sources` / `get_data_source` (`/data-sources`).
  - `collection_field_values(col_id, field="nd_id", ...)` — distinct values
    via `/results` (the REAL store), for client-side diffing ("which nd_ids
    does Crawlab already have?"). The recommended way to handle large exclude
    sets: pull existing values and compute the difference locally.
  - `filter_field_options(col_name, field, ...)` — server-side distinct via
    `/filters/{col}/{field}` (`$group`, no limit). Built-in store only.
- Generic read resources (each with `list_*` / `iter_*` / `get_*` where the
  backend supports it): projects, nodes, users (+ `get_current_user`),
  tags, gits, tokens, settings, plugins, roles, permissions, environments,
  notification settings.
- System & stats single-object reads: `system_info`, `stats_overview`,
  `stats_daily`.
- Spider source files (read): `list_spider_files`, `get_spider_file`,
  `get_spider_file_info`.
- Mutating actions (POST, no delete): `run_spider`, `restart_task`,
  `cancel_task`, `enable_schedule`, `disable_schedule`.
- `_request_raw` low-level helper for binary / file downloads.

### Discovered (verified against the backend source + live; in CLAUDE.md)
- **Results & data sources (corrects an earlier wrong "Bug F"):**
  `/results/{col_id}` returning `mongo: no documents in result` is NOT a
  breakage — `GetResultList` resolves the spider by `{col_id, data_source_id}`
  (defaulting the id to the zero ObjectID). When results live in an external
  data source you MUST pass `data_source_id`; with it, `list_results` &
  friends work (verified: 5644 rows). The production instance stores results
  in an external Mongo source.
- **Store split:** `export` and `/filters` have no `data_source_id` param and
  read only Crawlab's built-in MongoDB — a stale/partial copy on external-store
  instances. Use `/results` (with `data_source_id`) for the real data.
- **Bug E:** `/export/{type}` and `/results/{col_id}` filter through
  **opposite** operator namespaces: export needs regular `Op.*` (`Op.NIN`
  works; `MongoOp.*` fails), `/results` needs the `MongoOp.*` `$`-prefix hack
  (only `Op.EQ` works). Confirmed in `core/utils/mongo.go:GetMongoQuery`.
- **URL-length cap:** `conditions` travels in the query string; nginx returns
  HTTP 414 past ~8 KB (≈1–4k `IN`/`NIN` values) and httpx raises `InvalidURL`
  past ~60 KB. Flip large exclude sets to client-side diffing via
  `collection_field_values`.
- **Bug A is fixed upstream:** `getAll` applies the filter in the 2024
  monorepo source, but the deployed build still drops it under `all=1`. The
  `_list` workaround stays until the deployed Crawlab is upgraded.
- **Version string is static:** `/system-info` reports `v0.6.3` from
  `config.yml`, not the deployed commit — don't treat it as a code marker.
- Export `target` is the collection **name**, not its id; export ignores a
  `fields=` projection param (full rows are always returned).

### Security note
- `list_tokens` and `list_gits` / `get_git` return live secrets (JWTs, git
  credentials) in cleartext. Documented in docstrings, README and CLAUDE.md;
  never log or commit their output.

## [0.6.3.1] — 2026-05-25

Initial usable release. Verified end-to-end against a self-hosted Crawlab
`0.6.3` instance.

### Added
- `CrawlabClient` — sync `httpx`-based client. Token sent as the raw
  `Authorization` header value (no `Bearer` prefix).
- Spider endpoints: `list_spiders`, `iter_spiders`, `get_spider`.
- Schedule endpoints: `list_schedules`, `iter_schedules`, `get_schedule`.
  Shortcuts: `spider_id=`, `enabled=`.
- Task endpoints: `list_tasks`, `iter_tasks`, `get_task`.
  Shortcuts: `spider_id=`, `schedule_id=`, `status=`.
- Per-task data and logs: `get_task_data`, `iter_task_data`, `get_task_logs`.
- Spider-collection data: `list_results`, `iter_results`,
  `iter_results_excluding` against `GET /results/{col_id}` (undocumented in
  Swagger).
- Filtering DSL: `conditions=[{"key", "op", "value"}]` exposed on every
  list method.
- `Op` namespace with operator constants for the generic list endpoints
  (`EQ`, `NE`, `GT`, `GTE`, `LT`, `LTE`, `IN`, `NIN`, `CONTAINS`, `REGEX`,
  `SEARCH`, `NOT_SET`, `NOT_CONTAINS`).
- `MongoOp` namespace with `$`-prefixed operators specifically for the
  `/results/{col_id}` endpoint, which has a broken op translator (see
  "Backend bug workarounds" below).
- `fetch_all=True` flag on list methods. Uses the server's `all=1` short
  path when no filter is set, transparently falls back to client-side
  pagination when a filter is present.
- Exception hierarchy: `CrawlabError` → `CrawlabAuthError`,
  `CrawlabNotFoundError`, `CrawlabAPIError`.
- Three live-instance scripts under `scripts/`:
  - `smoke_test.py` — spider → task → data sanity check.
  - `dump_spider_tasks.py [spider_id]` — full per-task data dump for a
    spider.
  - `dump_schedule_tasks.py [schedule_id]` — full per-task data dump for
    a schedule's tasks (or every schedule if no id passed).
- `CLAUDE.md` — agent-facing project briefing covering architecture,
  endpoint coverage, and all discovered backend bugs with source-code
  citations.
- `README.md` — user-facing install instructions for private GitHub
  repositories (HTTPS+PAT and SSH), usage examples, and a summary of
  backend caveats.

### Backend bug workarounds

Documented in `CLAUDE.md` with citations into the Crawlab Go source:

- **`filter=` is not a real query parameter.** Crawlab uses `conditions=`
  (a JSON array of `{key, op, value}`). The wrapper only ever sends
  `conditions=`.
- **`all=1` silently drops `conditions`.** Verified against `/tasks`,
  `/spiders`, `/schedules`. The wrapper detects the combination and
  transparently switches to client-side pagination when a filter is set.
- **`/tasks/{id}/data` does not accept `conditions`.** The handler
  hard-codes the Mongo query to `{_tid: task_id}` and never reads the
  filter. To filter rows across tasks, use `list_results(col_id, ...)`.
- **`/results/{col_id}` has a broken op translator.** Only `eq` works
  through the regular `Op.*` constants; every other op produces
  `bson.M{op_string: value}` without the `$` Mongo expects. The wrapper
  exposes `MongoOp.*` (`$`-prefixed) operators that pass through the bug
  as valid Mongo expressions. `iter_results_excluding` uses server-side
  `$nin` by default and accepts `client_side=True` as a fallback if
  Crawlab ever patches the translator (which would turn `$nin` into
  `$$nin`).

### Project setup
- Build backend: `uv_build`. Layout: `src/`. Package name: `crawlab-api`.
- Runtime dep: `httpx>=0.27`. Optional `dev` extras: `pytest`, `respx`.
- Python `>=3.10`.
- `.env` (holding `CRAWLAB_BASE_URL` and `CRAWLAB_TOKEN` for live tests)
  added to `.gitignore`.
