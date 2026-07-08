from __future__ import annotations

import json
import time
from types import TracebackType
from typing import Any, Iterator

import httpx

from .conditions import MongoOp, Op
from .exceptions import (
    CrawlabAPIError,
    CrawlabAuthError,
    CrawlabNotFoundError,
)
from .models import DataCollection

JsonDict = dict[str, Any]


class CrawlabClient:
    """Synchronous client for the self-hosted Crawlab API.

    Scope is read-heavy and covers nearly the whole Crawlab API surface:
    data collections, spiders, schedules, tasks, results / logs, projects,
    nodes, users, tags, gits, tokens, settings, plugins, roles, permissions,
    environments, notification settings, system info and stats, bulk export
    (CSV / JSON), and spider source files. A small set of mutating actions is
    exposed too (``run_spider``, ``restart_task``, ``cancel_task``,
    ``enable_schedule`` / ``disable_schedule``).

    DELETE-style operations are deliberately NOT implemented, to avoid
    accidental data loss. The ``tokens`` and ``gits`` endpoints return live
    secrets (JWTs, git credentials) in cleartext — never log their output.

    Filtering on list endpoints
    ---------------------------
    Crawlab list endpoints (``/data/collections``, ``/spiders``, ``/tasks``, ``/schedules`` …) accept a
    ``conditions`` query parameter — a JSON-encoded array of objects with
    ``key``, ``op``, ``value``. This client exposes that as a typed ``list[dict]``
    via the ``conditions=`` keyword on every ``list_*`` / ``iter_*`` method.

    Convenience keywords like ``spider_id=``, ``schedule_id=``, ``status=`` are
    merged into ``conditions`` as ``eq`` comparisons.

    For the full operator list, see :mod:`crawlab_api.conditions` and the
    ``Op`` class — values mirror the Crawlab Go backend
    (``core/constants/filter.go``).

    The Crawlab Swagger UI does not document ``conditions`` or ``all``;
    the source of truth is the Crawlab backend.

    Listing everything
    ------------------
    Pass ``fetch_all=True`` to return every row without manual pagination.

    Implementation detail (and a workaround for a Crawlab backend bug): the
    server supports an ``all=1`` query parameter that skips pagination
    (``core/controllers/base_v2.go``), but in that code path the backend
    silently ignores the ``conditions`` filter and returns every row in the
    collection. To stay safe, this client uses ``all=1`` only when there is
    no filter to apply; otherwise it falls back to client-side pagination
    so filters actually work.

    Alternatively use the ``iter_*`` methods to stream pages explicitly.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        data_source_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")

        # Default data source for /results reads. REQUIRED when a spider's
        # results live in an EXTERNAL data source: GetResultList looks up the
        # spider by {col_id, data_source_id}, defaulting data_source_id to the
        # zero ObjectID, so without the real id the lookup fails with HTTP 500
        # "mongo: no documents in result" (see CLAUDE.md "Results & data
        # sources"). Discover ids via list_data_sources(). Per-call
        # data_source_id= overrides this default.
        self._data_source_id = data_source_id

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": token,
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    # ----- context manager / lifecycle ---------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CrawlabClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ----- low-level request ------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: JsonDict | None = None,
        json_body: Any = None,
    ) -> Any:
        response = self._client.request(
            method,
            path,
            params=self._clean_params(params),
            json=json_body,
        )
        return self._handle(response)

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: JsonDict | None = None,
    ) -> bytes:
        """Like :meth:`_request` but returns the raw response body as bytes.

        Used for binary / file downloads (e.g. export download) where the
        server does not return the usual ``{"data": ...}`` JSON envelope.
        Error responses are still routed through :meth:`_handle`, which
        raises the appropriate ``CrawlabError``.
        """
        response = self._client.request(
            method,
            path,
            params=self._clean_params(params),
        )
        if response.is_success:
            return response.content
        self._handle(response)  # raises
        return b""  # unreachable; keeps type checkers happy

    @staticmethod
    def _clean_params(params: JsonDict | None) -> JsonDict | None:
        if not params:
            return None
        return {k: v for k, v in params.items() if v is not None}

    @staticmethod
    def _handle(response: httpx.Response) -> Any:
        if response.is_success:
            if not response.content:
                return None
            try:
                payload = response.json()
            except json.JSONDecodeError:
                return response.text
            if isinstance(payload, dict):
                # Crawlab отдаёт HTTP 200 даже на ошибках (протухший/пустой
                # токен, внутренние сбои), сигнализируя их непустым полем
                # `error` при `data: null`. Без этой проверки unauthorized
                # выглядел бы как «пустой результат», а не ошибка авторизации.
                error = payload.get("error")
                if error:
                    message = str(error)
                    if any(w in message.lower() for w in ("unauthorized", "forbidden")):
                        raise CrawlabAuthError(f"[{response.status_code}] {message}")
                    raise CrawlabAPIError(response.status_code, message, payload)
                if "data" in payload:
                    return payload["data"]
            return payload

        try:
            payload = response.json()
            message = payload.get("error") or payload.get("message") or response.text
        except json.JSONDecodeError:
            payload = None
            message = response.text or response.reason_phrase

        status = response.status_code
        if status in (401, 403):
            raise CrawlabAuthError(f"[{status}] {message}")
        if status == 404:
            raise CrawlabNotFoundError(f"[{status}] {message}")
        raise CrawlabAPIError(status, str(message), payload)

    # ----- internal helpers --------------------------------------------

    def _list(
        self,
        path: str,
        *,
        page: int,
        size: int,
        conditions: list[JsonDict] | None,
        sort: list[JsonDict] | None,
        fetch_all: bool,
    ) -> list[JsonDict]:
        if fetch_all and not conditions:
            params: JsonDict = {"all": "1"}
            if sort:
                params["sort"] = json.dumps(sort)
            return self._request("GET", path, params=params) or []

        if fetch_all:
            # Crawlab ignores `conditions` when `all=1` is set, so we paginate
            # client-side to keep the filter effective.
            results: list[JsonDict] = []
            page_size = 100
            current_page = 1
            while True:
                batch = self._list(
                    path,
                    page=current_page,
                    size=page_size,
                    conditions=conditions,
                    sort=sort,
                    fetch_all=False,
                )
                if not batch:
                    break
                results.extend(batch)
                if len(batch) < page_size:
                    break
                current_page += 1
            return results

        params = {"page": page, "size": size}
        if conditions:
            params["conditions"] = json.dumps(conditions)
        if sort:
            params["sort"] = json.dumps(sort)
        return self._request("GET", path, params=params) or []

    @staticmethod
    def _paginate(
        fetch_page: Any,
        size: int,
    ) -> Iterator[JsonDict]:
        page = 1
        while True:
            batch = fetch_page(page)
            if not batch:
                return
            yield from batch
            if len(batch) < size:
                return
            page += 1

    @staticmethod
    def _validate_dedup(dedup: JsonDict | None) -> None:
        """Validate deduplication config.

        Raises ValueError if enabled=True but keys list is empty or missing.
        This prevents silent dedup disabling on the server side.
        """
        if dedup is None:
            return
        if dedup.get("enabled") is True:
            keys = dedup.get("keys", [])
            if not keys or len(keys) == 0:
                raise ValueError(
                    "dedup.keys cannot be empty when dedup.enabled=True. "
                    "Specify at least one field name to deduplicate on, "
                    "or set dedup.enabled=False."
                )

    def _get_one(self, resource: str, resource_id: str) -> JsonDict:
        """GET a single object from a generic ``/{resource}/{id}`` endpoint."""
        return self._request("GET", f"/{resource}/{resource_id}")

    # ----- data collections -------------------------------------------

    def list_data_collections(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[DataCollection]:
        """List data collections (MongoDB collections registered in Crawlab).

        Data collections store scraped results. Pass ``conditions=`` for filtering
        by name or other fields. Set ``fetch_all=True`` to return every collection.

        Returns :class:`~crawlab_api.models.DataCollection` objects. The id is
        exposed as ``.id`` (the Crawlab API field is ``_id``). Accessing an
        attribute the object does not define raises ``AttributeError`` rather
        than silently returning ``None``.
        """
        raw = self._list(
            "/data/collections",
            page=page,
            size=size,
            conditions=conditions,
            sort=sort,
            fetch_all=fetch_all,
        )
        return [DataCollection.from_api(dc) for dc in raw]

    def iter_data_collections(
        self,
        *,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
    ) -> Iterator[DataCollection]:
        """Iterate over every data collection across all pages."""
        return self._paginate(
            lambda p: self.list_data_collections(page=p, size=size, conditions=conditions),
            size,
        )

    def get_data_collection(self, collection_id: str) -> DataCollection:
        """Fetch a single data collection by id."""
        return DataCollection.from_api(
            self._request("GET", f"/data/collections/{collection_id}")
        )

    def create_data_collection(
        self,
        name: str,
        *,
        fields: list[JsonDict] | None = None,
        dedup: JsonDict | None = None,
    ) -> JsonDict:
        """Create a new data collection.

        Args:
            name: human-readable name for the collection.
            fields: optional list of field definitions. Each field should have
                ``name`` and ``type`` keys.
            dedup: optional deduplication config dict with keys:
                - ``enabled`` (bool): enable deduplication.
                - ``keys`` (list[str]): field names to use as dedup keys
                  (required if enabled=True).
                - ``type`` (str): dedup type ("ignore" to skip duplicates,
                  "overwrite" to update existing).

        Raises ValueError if dedup.enabled=True but keys is empty or missing.

        Returns the created collection dict (including id and timestamps).
        """
        self._validate_dedup(dedup)
        payload: JsonDict = {"name": name}
        if fields is not None:
            payload["fields"] = fields
        if dedup is not None:
            payload["dedup"] = dedup
        return self._request("POST", "/data/collections", json_body=payload)

    def update_data_collection(
        self,
        collection_id: str,
        *,
        name: str | None = None,
        fields: list[JsonDict] | None = None,
        dedup: JsonDict | None = None,
    ) -> JsonDict:
        """Update an existing data collection.

        Pass only the fields you want to change; omitted fields are left alone.

        Args:
            collection_id: id of the collection to update.
            name: new collection name (optional).
            fields: new field definitions (optional).
            dedup: new dedup config (optional). Same validation as
                create_data_collection applies here.

        Raises ValueError if dedup.enabled=True but keys is empty or missing.

        Returns the updated collection dict.
        """
        self._validate_dedup(dedup)
        payload: JsonDict = {}
        if name is not None:
            payload["name"] = name
        if fields is not None:
            payload["fields"] = fields
        if dedup is not None:
            payload["dedup"] = dedup
        return self._request(
            "PUT",
            f"/data/collections/{collection_id}",
            json_body=payload,
        )

    # ----- spiders ----------------------------------------------------

    def list_spiders(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List spiders.

        Pass ``conditions=[{"key": "...", "op": Op.EQ, "value": "..."}]`` for
        filtering. Set ``fetch_all=True`` to return every row in one call.
        """
        return self._list(
            "/spiders",
            page=page,
            size=size,
            conditions=conditions,
            sort=sort,
            fetch_all=fetch_all,
        )

    def iter_spiders(
        self,
        *,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every spider across all pages."""
        return self._paginate(
            lambda p: self.list_spiders(page=p, size=size, conditions=conditions),
            size,
        )

    def get_spider(self, spider_id: str) -> JsonDict:
        """Fetch a single spider by id."""
        return self._request("GET", f"/spiders/{spider_id}")

    # ----- schedules --------------------------------------------------

    def list_schedules(
        self,
        *,
        spider_id: str | None = None,
        enabled: bool | None = None,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List schedules.

        ``spider_id=`` and ``enabled=`` are convenience shortcuts merged into
        ``conditions`` as ``eq`` comparisons. A schedule represents a cron-driven
        recurring trigger for a spider; each fire creates a Task with
        ``schedule_id`` set to this schedule's id.
        """
        merged = list(conditions or [])
        if spider_id is not None:
            merged.append({"key": "spider_id", "op": Op.EQ, "value": spider_id})
        if enabled is not None:
            merged.append({"key": "enabled", "op": Op.EQ, "value": enabled})
        return self._list(
            "/schedules",
            page=page,
            size=size,
            conditions=merged or None,
            sort=sort,
            fetch_all=fetch_all,
        )

    def iter_schedules(
        self,
        *,
        spider_id: str | None = None,
        enabled: bool | None = None,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every schedule across all pages."""
        return self._paginate(
            lambda p: self.list_schedules(
                spider_id=spider_id,
                enabled=enabled,
                page=p,
                size=size,
                conditions=conditions,
            ),
            size,
        )

    def get_schedule(self, schedule_id: str) -> JsonDict:
        """Fetch a single schedule by id."""
        return self._request("GET", f"/schedules/{schedule_id}")

    # ----- tasks ------------------------------------------------------

    def list_tasks(
        self,
        *,
        spider_id: str | None = None,
        schedule_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List tasks.

        ``spider_id=``, ``schedule_id=`` and ``status=`` are convenience
        shortcuts merged into ``conditions`` as ``eq`` comparisons.
        """
        merged = list(conditions or [])
        if spider_id is not None:
            merged.append({"key": "spider_id", "op": Op.EQ, "value": spider_id})
        if schedule_id is not None:
            merged.append({"key": "schedule_id", "op": Op.EQ, "value": schedule_id})
        if status is not None:
            merged.append({"key": "status", "op": Op.EQ, "value": status})
        return self._list(
            "/tasks",
            page=page,
            size=size,
            conditions=merged or None,
            sort=sort,
            fetch_all=fetch_all,
        )

    def iter_tasks(
        self,
        *,
        spider_id: str | None = None,
        schedule_id: str | None = None,
        status: str | None = None,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every task across all pages."""
        return self._paginate(
            lambda p: self.list_tasks(
                spider_id=spider_id,
                schedule_id=schedule_id,
                status=status,
                page=p,
                size=size,
                conditions=conditions,
            ),
            size,
        )

    def get_task(self, task_id: str) -> JsonDict:
        """Fetch a single task by id."""
        return self._request("GET", f"/tasks/{task_id}")

    # ----- task data --------------------------------------------------

    def get_task_data(
        self,
        task_id: str,
        *,
        page: int = 1,
        size: int = 100,
    ) -> list[JsonDict]:
        """Fetch one page of result rows produced by a single task.

        Backed by ``GET /tasks/{task_id}/data``. The backend hard-codes the
        Mongo query to ``{_tid: task_id}`` and does NOT read ``conditions``
        from the query string (``core/controllers/task_v2.go:GetTaskData``).
        To filter rows by an arbitrary field across all tasks of a spider,
        use :meth:`list_results` against the spider's ``col_id`` instead.
        """
        return self._request(
            "GET",
            f"/tasks/{task_id}/data",
            params={"page": page, "size": size},
        ) or []

    def iter_task_data(
        self,
        task_id: str,
        *,
        size: int = 100,
    ) -> Iterator[JsonDict]:
        """Iterate over every result row across all pages."""
        return self._paginate(
            lambda p: self.get_task_data(task_id, page=p, size=size),
            size,
        )

    # ----- collection results (cross-task data of a spider) ------------

    def list_results(
        self,
        col_id: str,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        data_source_id: str | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List rows from a spider's data collection.

        ``col_id`` is the spider's ``col_id`` field (the MongoDB collection
        backing the spider's results). The endpoint is undocumented in Swagger
        but maps to ``GET /results/{col_id}`` in the Crawlab backend
        (``core/controllers/result_v2.go``).

        Filtering on this endpoint is quirky — see :class:`MongoOp` and the
        notes in CLAUDE.md. In short:

        * ``Op.EQ`` works as expected.
        * Other ``Op.*`` values silently match nothing (Crawlab backend bug:
          translator does not add the ``$`` prefix Mongo requires).
        * As a workaround, pass operators with the ``$`` prefix
          (``MongoOp.NIN``, ``MongoOp.IN``, ``MongoOp.GT`` …); they pass
          through the default branch of the translator and produce valid
          Mongo expressions.

        For excluding rows by a list of values, prefer
        :meth:`iter_results_excluding` which encapsulates the ``$nin`` hack.

        ``data_source_id`` — REQUIRED when the results live in an external data
        source. ``GetResultList`` finds the spider by ``{col_id,
        data_source_id}`` (defaulting the id to the zero ObjectID), so without
        the correct id the lookup raises HTTP 500 "mongo: no documents in
        result". Defaults to the client-level ``data_source_id`` passed to the
        constructor; discover ids with :meth:`list_data_sources`.
        """
        data_source_id = data_source_id or self._data_source_id
        params: JsonDict
        if fetch_all and not conditions:
            params = {"all": "1"}
        else:
            params = {"page": page, "size": size}
            if conditions:
                params["conditions"] = json.dumps(conditions)
        if data_source_id:
            params["data_source_id"] = data_source_id

        if fetch_all and conditions:
            results: list[JsonDict] = []
            page_size = 100
            current_page = 1
            while True:
                batch = self.list_results(
                    col_id,
                    page=current_page,
                    size=page_size,
                    conditions=conditions,
                    data_source_id=data_source_id,
                )
                if not batch:
                    break
                results.extend(batch)
                if len(batch) < page_size:
                    break
                current_page += 1
            return results

        return self._request("GET", f"/results/{col_id}", params=params) or []

    def iter_results(
        self,
        col_id: str,
        *,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        data_source_id: str | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate every row of a spider's data collection across all pages."""
        return self._paginate(
            lambda p: self.list_results(
                col_id,
                page=p,
                size=size,
                conditions=conditions,
                data_source_id=data_source_id,
            ),
            size,
        )

    def iter_results_excluding(
        self,
        col_id: str,
        *,
        field: str,
        exclude: set[Any] | list[Any],
        size: int = 100,
        data_source_id: str | None = None,
        client_side: bool = False,
    ) -> Iterator[JsonDict]:
        """Stream rows whose ``field`` value is NOT in ``exclude``.

        By default uses **server-side** ``$nin`` filtering via :class:`MongoOp`
        (the ``$``-prefix hack — see :meth:`list_results`), which is fast and
        avoids transferring the excluded rows over the network at all.

        Pass ``client_side=True`` to fall back to fetching every row and
        filtering in Python — useful if Crawlab ever patches the translator
        and the ``$``-prefix hack stops working, or if ``exclude`` is so large
        that sending it in a query becomes impractical (Mongo's per-query
        limit is 16 MB).

        Empty ``exclude`` is handled efficiently — it just streams everything.
        """
        exclude_list = list(set(exclude))

        if not exclude_list:
            yield from self.iter_results(
                col_id,
                size=size,
                data_source_id=data_source_id,
            )
            return

        if client_side:
            exclude_set = set(exclude_list)
            for row in self.iter_results(
                col_id,
                size=size,
                data_source_id=data_source_id,
            ):
                if row.get(field) not in exclude_set:
                    yield row
            return

        yield from self.iter_results(
            col_id,
            size=size,
            conditions=[{"key": field, "op": MongoOp.NIN, "value": exclude_list}],
            data_source_id=data_source_id,
        )

    def get_task_logs(
        self,
        task_id: str,
        *,
        page: int = 1,
        size: int = 1000,
    ) -> list[str]:
        """Fetch task log lines."""
        data = self._request(
            "GET",
            f"/tasks/{task_id}/logs",
            params={"page": page, "size": size},
        )
        if data is None:
            return []
        if isinstance(data, list):
            return [line if isinstance(line, str) else str(line) for line in data]
        if isinstance(data, str):
            return data.splitlines()
        return [str(data)]

    # ==================================================================
    # Generic read-only resources (list / iter / get)
    #
    # Every endpoint below is routed through the generic list controller
    # (``core/controllers/base_v2.go``), so it accepts the same
    # ``conditions`` DSL and is subject to Bug A (``all=1`` drops the
    # filter), which ``_list`` already works around. ``get_*`` reads a
    # single object via ``/{resource}/{id}``.
    #
    # NOTE: scope is read-only and intentionally excludes DELETE. Mutating
    # POST/PUT actions live further down ("Actions"). The ``tokens`` and
    # ``gits`` endpoints return SECRETS in cleartext (JWTs, git
    # credentials) — never log or commit their output.
    # ==================================================================

    def list_projects(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List projects. Each project groups spiders (``spiders`` = count)."""
        return self._list(
            "/projects", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def iter_projects(
        self, *, size: int = 100, conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every project across all pages."""
        return self._paginate(
            lambda p: self.list_projects(page=p, size=size, conditions=conditions),
            size,
        )

    def get_project(self, project_id: str) -> JsonDict:
        """Fetch a single project by id."""
        return self._get_one("projects", project_id)

    def list_nodes(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List worker / master nodes (``is_master``, ``status``, runners…)."""
        return self._list(
            "/nodes", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def iter_nodes(
        self, *, size: int = 100, conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every node across all pages."""
        return self._paginate(
            lambda p: self.list_nodes(page=p, size=size, conditions=conditions),
            size,
        )

    def get_node(self, node_id: str) -> JsonDict:
        """Fetch a single node by id."""
        return self._get_one("nodes", node_id)

    def list_users(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List users (``username``, ``role``, ``email``)."""
        return self._list(
            "/users", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def iter_users(
        self, *, size: int = 100, conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every user across all pages."""
        return self._paginate(
            lambda p: self.list_users(page=p, size=size, conditions=conditions),
            size,
        )

    def get_user(self, user_id: str) -> JsonDict:
        """Fetch a single user by id."""
        return self._get_one("users", user_id)

    def get_current_user(self) -> JsonDict:
        """Fetch the user owning the API token (``GET /users/me``)."""
        return self._request("GET", "/users/me")

    def list_tags(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List tags (free-form labels attached to spiders / other entities)."""
        return self._list(
            "/tags", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def iter_tags(
        self, *, size: int = 100, conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every tag across all pages."""
        return self._paginate(
            lambda p: self.list_tags(page=p, size=size, conditions=conditions),
            size,
        )

    def list_gits(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List git repositories registered for spider source sync.

        WARNING: the returned objects include cleartext git credentials
        (``username`` / ``password``, e.g. a GitHub PAT). Never log or commit.
        """
        return self._list(
            "/gits", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def iter_gits(
        self, *, size: int = 100, conditions: list[JsonDict] | None = None,
    ) -> Iterator[JsonDict]:
        """Iterate over every git repo across all pages (see secret warning)."""
        return self._paginate(
            lambda p: self.list_gits(page=p, size=size, conditions=conditions),
            size,
        )

    def get_git(self, git_id: str) -> JsonDict:
        """Fetch a single git repo by id (includes cleartext credentials)."""
        return self._get_one("gits", git_id)

    def list_tokens(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List API tokens.

        WARNING: each object contains the raw JWT in the ``token`` field —
        a working credential. Never log or commit the output.
        """
        return self._list(
            "/tokens", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def list_settings(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List server settings (``key`` / ``value`` config rows)."""
        return self._list(
            "/settings", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def get_setting(self, setting_id: str) -> JsonDict:
        """Fetch a single setting by id."""
        return self._get_one("settings", setting_id)

    def list_plugins(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List installed plugins."""
        return self._list(
            "/plugins", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def get_plugin(self, plugin_id: str) -> JsonDict:
        """Fetch a single plugin by id."""
        return self._get_one("plugins", plugin_id)

    def list_roles(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List roles (RBAC; empty on Community edition without RBAC config)."""
        return self._list(
            "/roles", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def list_permissions(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List permissions (RBAC)."""
        return self._list(
            "/permissions", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def list_environments(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List environment variables shared with spider runs."""
        return self._list(
            "/environments", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def get_environment(self, environment_id: str) -> JsonDict:
        """Fetch a single environment variable by id."""
        return self._get_one("environments", environment_id)

    def list_data_sources(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List configured data sources (``GET /data-sources``).

        A data source is an external store (MongoDB / PostgreSQL / …) where a
        spider writes its results. The ``_id`` of the relevant source is what
        you pass as ``data_source_id`` to :meth:`list_results` /
        :meth:`iter_results` / :meth:`iter_results_excluding` (or to the client
        constructor). Crawlab's own built-in MongoDB is NOT listed here and is
        addressed by omitting ``data_source_id``.
        """
        return self._list(
            "/data-sources", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    def get_data_source(self, data_source_id: str) -> JsonDict:
        """Fetch a single data source by id."""
        return self._get_one("data-sources", data_source_id)

    def list_notification_settings(
        self,
        *,
        page: int = 1,
        size: int = 100,
        conditions: list[JsonDict] | None = None,
        sort: list[JsonDict] | None = None,
        fetch_all: bool = False,
    ) -> list[JsonDict]:
        """List notification settings (``GET /notifications/settings``)."""
        return self._list(
            "/notifications/settings", page=page, size=size,
            conditions=conditions, sort=sort, fetch_all=fetch_all,
        )

    # ==================================================================
    # System & statistics (single-object reads)
    # ==================================================================

    def system_info(self) -> JsonDict:
        """Server edition + version (``GET /system-info``)."""
        return self._request("GET", "/system-info")

    def stats_overview(self) -> JsonDict:
        """Dashboard totals: spiders, tasks, results, error_tasks, … ."""
        return self._request("GET", "/stats/overview")

    def stats_daily(self) -> list[JsonDict]:
        """Per-day task / result counts (``GET /stats/daily``)."""
        return self._request("GET", "/stats/daily") or []

    # ==================================================================
    # Export — bulk extraction of a data collection to CSV / JSON
    #
    # Flow (verified against Crawlab 0.6.3):
    #   POST /export/{type}?target=<collection_name>[&conditions=<json>]
    #        -> returns an export id (a UUID string)
    #   GET  /export/{type}/{id}            -> status object (status: running|finished)
    #   GET  /export/{type}/{id}/download   -> the file bytes
    #
    # ``target`` is the data collection NAME (e.g. "products"), not its id.
    # ``conditions`` accepts the same DSL as list endpoints; the export
    # controller honours it server-side, so you can export only the rows
    # you want (e.g. exclude already-downloaded nd_ids).
    #
    # STORE GOTCHA (verified against export_v2.go + live): PostExport reads
    # ONLY ``target`` and ``conditions`` — it has NO ``data_source_id``
    # parameter, so it always reads Crawlab's BUILT-IN MongoDB. If a spider
    # writes to an EXTERNAL data source, export sees only the built-in store
    # (often a stale/partial copy), NOT the real results. For externally
    # stored data, read via :meth:`list_results` with ``data_source_id``.
    #
    # OPERATOR GOTCHA (verified June 2026): export uses the PROPER filter
    # translator — use the regular ``Op.*`` constants here (``Op.NIN``,
    # ``Op.IN``, ``Op.GT`` … all work). This is the OPPOSITE of
    # ``/results/{col_id}``, where only ``Op.EQ`` works and you must use the
    # ``MongoOp.*`` ($-prefixed) hack. Passing ``MongoOp.*`` to export makes
    # the job fail server-side (status="error"). See CLAUDE.md Bug E.
    # ==================================================================

    EXPORT_CSV = "csv"
    EXPORT_JSON = "json"

    def create_export(
        self,
        export_type: str,
        target: str,
        *,
        conditions: list[JsonDict] | None = None,
    ) -> str:
        """Start an export job; returns its id (string).

        ``export_type`` is ``"csv"`` or ``"json"`` (see :attr:`EXPORT_CSV` /
        :attr:`EXPORT_JSON`). ``target`` is the data collection *name*.

        Use the regular ``Op.*`` operators in ``conditions`` (NOT
        ``MongoOp.*`` — those make the export fail server-side).
        """
        params: JsonDict = {"target": target}
        if conditions:
            params["conditions"] = json.dumps(conditions)
        return self._request("POST", f"/export/{export_type}", params=params)

    def get_export(self, export_type: str, export_id: str) -> JsonDict:
        """Fetch export job status (``status`` is ``running`` / ``finished``)."""
        return self._request("GET", f"/export/{export_type}/{export_id}")

    def download_export(self, export_type: str, export_id: str) -> bytes:
        """Download a finished export's file as raw bytes."""
        return self._request_raw(
            "GET", f"/export/{export_type}/{export_id}/download"
        )

    def export_collection(
        self,
        export_type: str,
        target: str,
        *,
        conditions: list[JsonDict] | None = None,
        poll_interval: float = 0.5,
        timeout: float = 120.0,
    ) -> bytes:
        """Convenience: start an export, wait until finished, return its bytes.

        Combines :meth:`create_export`, polling :meth:`get_export` until its
        ``status`` is ``finished``, then :meth:`download_export`. Raises
        :class:`TimeoutError` if it does not finish within ``timeout`` seconds.

        Pass ``conditions`` to export only matching rows (server-side filter).
        """
        export_id = self.create_export(
            export_type, target, conditions=conditions
        )
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_export(export_type, export_id)
            state = status.get("status")
            if state == "finished":
                break
            if state == "error":
                raise CrawlabAPIError(
                    500,
                    f"export {export_id} failed server-side "
                    f"(status={state!r}); check the conditions/operators",
                    status,
                )
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"export {export_id} did not finish within {timeout}s "
                    f"(last status={status.get('status')!r})"
                )
            time.sleep(poll_interval)
        return self.download_export(export_type, export_id)

    def export_collection_excluding(
        self,
        export_type: str,
        target: str,
        *,
        field: str,
        exclude: set[Any] | list[Any],
        poll_interval: float = 0.5,
        timeout: float = 120.0,
    ) -> bytes:
        """Export a collection, omitting rows whose ``field`` is in ``exclude``.

        This is the export-based counterpart of :meth:`iter_results_excluding`
        and the recommended way to pull "everything I haven't downloaded yet"
        (e.g. ``field="nd_id"``, ``exclude={already_seen_ids}``).

        Unlike ``/results/{col_id}``, export honours the proper ``Op.NIN``
        operator server-side, so the excluded rows never leave the server and
        you get a ready-to-process CSV / JSON file. An empty ``exclude``
        exports the whole collection.
        """
        exclude_list = list(set(exclude))
        conditions = (
            [{"key": field, "op": Op.NIN, "value": exclude_list}]
            if exclude_list
            else None
        )
        return self.export_collection(
            export_type,
            target,
            conditions=conditions,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    def collection_field_values(
        self,
        col_id: str,
        field: str = "nd_id",
        *,
        conditions: list[JsonDict] | None = None,
        data_source_id: str | None = None,
        size: int = 500,
    ) -> set[Any]:
        """Return the set of distinct values of ``field`` present in a collection.

        This is the answer to "which ``nd_id``s does Crawlab already have?".
        Instead of pushing a huge exclude list TO the server (which overflows
        the URL — see :meth:`iter_results_excluding`), pull the values that
        already exist and diff client-side::

            have = client.collection_field_values(spider["col_id"], "nd_id")
            to_download = my_candidate_ids - have   # plain set difference

        Reads via :meth:`iter_results` (``GET /results/{col_id}``), so it sees
        the SAME store as your scraped data — pass ``data_source_id`` (or set it
        on the client) when results live in an external data source, otherwise
        the read fails (see :meth:`list_results`). ``col_id`` is the spider's
        ``col_id``, NOT a collection name.

        Crawlab has no server-side field projection and no ``distinct`` for
        external data sources, so this streams full rows and collects the field
        in Python. For collections held in Crawlab's built-in MongoDB you can
        use the cheaper server-side :meth:`filter_field_options` instead.
        Pass ``conditions`` (use :class:`MongoOp` on ``/results``) to narrow
        server-side first.
        """
        return {
            row[field]
            for row in self.iter_results(
                col_id,
                size=size,
                conditions=conditions,
                data_source_id=data_source_id,
            )
            if isinstance(row, dict) and field in row
        }

    def filter_field_options(
        self,
        col_name: str,
        field: str = "nd_id",
        *,
        conditions: list[JsonDict] | None = None,
    ) -> list[Any]:
        """Distinct values of ``field`` via ``GET /filters/{col}/{field}``.

        Server-side ``distinct`` (a Mongo ``$group`` aggregation), sorted, with
        NO limit — cheap, since rows are not transferred. Returns the list of
        distinct ``value``s (the endpoint yields ``[{"value", "label"}]``; this
        unwraps to just the values).

        IMPORTANT caveats (verified against the backend source,
        ``core/controllers/filter_v2.go``):

        * ``col_name`` is the physical collection NAME, not a col_id.
        * The handler queries Crawlab's BUILT-IN MongoDB only — it has no
          ``data_source_id`` parameter. If your results live in an external
          data source, this returns values from the built-in store (often a
          stale/partial copy), NOT your real data. In that case use
          :meth:`collection_field_values` instead.
        * ``conditions`` here use the proper generic filter (regular ``Op.*``),
          not the ``MongoOp`` ``$``-prefix hack that ``/results`` needs.
        """
        path = f"/filters/{col_name}/{field}"
        params: JsonDict = {}
        if conditions:
            params["conditions"] = json.dumps(conditions)
        data = self._request("GET", path, params=params or None) or []
        return [
            opt.get("value") if isinstance(opt, dict) else opt
            for opt in data
        ]

    # ==================================================================
    # Spider source files (read-only)
    # ==================================================================

    def list_spider_files(self, spider_id: str) -> list[JsonDict]:
        """List the spider's source file tree (``GET /spiders/{id}/files/list``).

        Returns a nested tree; each node has ``name``, ``path``, ``full_path``,
        ``is_dir``, ``file_size``, ``md5`` and (for directories) ``children``.
        """
        return self._request("GET", f"/spiders/{spider_id}/files/list") or []

    def get_spider_file(self, spider_id: str, path: str) -> Any:
        """Fetch the content of a single spider source file.

        ``path`` is the file's ``path`` from :meth:`list_spider_files`
        (e.g. ``/courts/main.py``). Backed by
        ``GET /spiders/{id}/files/get?path=...``.
        """
        return self._request(
            "GET", f"/spiders/{spider_id}/files/get", params={"path": path}
        )

    def get_spider_file_info(self, spider_id: str, path: str) -> JsonDict:
        """Fetch metadata for a single spider file (``…/files/info?path=``)."""
        return self._request(
            "GET", f"/spiders/{spider_id}/files/info", params={"path": path}
        )

    # ==================================================================
    # Actions (mutating POST — deliberately NO delete)
    #
    # These create or change server state. They are kept apart from the
    # read methods above so callers can clearly see where side effects
    # begin. DELETE-style operations are intentionally not implemented.
    # ==================================================================

    def run_spider(
        self,
        spider_id: str,
        *,
        mode: str | None = None,
        node_ids: list[str] | None = None,
        cmd: str | None = None,
        param: str | None = None,
        priority: int | None = None,
        schedule_id: str | None = None,
        **extra: Any,
    ) -> Any:
        """Trigger a spider run (``POST /spiders/{id}/run``); creates Task(s).

        Body fields match the backend ``SpiderRunOptions`` struct
        (``core/interfaces/spider_service_options.go``, verified): ``mode``
        (``random`` / ``all_nodes`` / ``selected_node_tags`` /
        ``selected_nodes``), ``node_ids``, ``cmd`` (override run command),
        ``param`` (extra CLI params), ``priority``, ``schedule_id``. All are
        optional. Returns whatever the backend reports for the launched run.

        NOTE: this is a real side effect — it enqueues task(s) on the server.
        """
        body: JsonDict = dict(extra)
        if mode is not None:
            body["mode"] = mode
        if node_ids is not None:
            body["node_ids"] = node_ids
        if cmd is not None:
            body["cmd"] = cmd
        if param is not None:
            body["param"] = param
        if priority is not None:
            body["priority"] = priority
        if schedule_id is not None:
            body["schedule_id"] = schedule_id
        return self._request("POST", f"/spiders/{spider_id}/run", json_body=body)

    def restart_task(self, task_id: str) -> Any:
        """Re-run a task (``POST /tasks/{id}/restart``)."""
        return self._request("POST", f"/tasks/{task_id}/restart", json_body={})

    def cancel_task(self, task_id: str) -> Any:
        """Cancel a running task (``POST /tasks/{id}/cancel``)."""
        return self._request("POST", f"/tasks/{task_id}/cancel", json_body={})

    def enable_schedule(self, schedule_id: str) -> Any:
        """Enable a schedule's cron trigger (``POST /schedules/{id}/enable``)."""
        return self._request(
            "POST", f"/schedules/{schedule_id}/enable", json_body={}
        )

    def disable_schedule(self, schedule_id: str) -> Any:
        """Disable a schedule's cron trigger (``POST /schedules/{id}/disable``)."""
        return self._request(
            "POST", f"/schedules/{schedule_id}/disable", json_body={}
        )
