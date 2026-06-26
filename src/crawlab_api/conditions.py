"""Crawlab listing-endpoint filter DSL.

Crawlab list endpoints (e.g. ``/spiders``, ``/tasks``, ``/schedules``) accept a
``conditions`` query parameter — a JSON-encoded array of objects, each shaped
like::

    {"key": "<field>", "op": "<operator>", "value": <value>}

Multiple objects in the array are AND-ed together server-side. The supported
operators are mirrored here from the Crawlab Go backend
(``core/constants/filter.go``):

    Op.EQ           "eq"    equal
    Op.NE           "ne"    not equal
    Op.GT           "gt"    greater than
    Op.GTE          "gte"   greater than or equal
    Op.LT           "lt"    less than
    Op.LTE          "lte"   less than or equal
    Op.IN           "in"    value in list
    Op.NIN          "nin"   value not in list
    Op.CONTAINS     "c"     substring contains
    Op.NOT_CONTAINS "nc"    substring does not contain
    Op.REGEX        "r"     regex match
    Op.SEARCH       "s"     full-text search
    Op.NOT_SET      "ns"    field not set

The Crawlab backend automatically converts string ``value`` fields that look
like 24-character hex into MongoDB ``ObjectID`` before querying, so passing
spider / task / schedule ids as plain strings just works.

The Crawlab Swagger UI does not document this parameter — it is part of the
generic backend list handler (``core/controllers/base_v2.go``) and applies to
all listing endpoints.
"""

from __future__ import annotations


class Op:
    """Crawlab filter operators for the generic list endpoints.

    Use these on ``/spiders``, ``/tasks``, ``/schedules`` and any other endpoint
    routed through ``GetFilterQuery`` + ``FilterToQuery`` server-side. The
    backend properly translates each op to the ``$``-prefixed Mongo equivalent.

    Do NOT use these on ``/results/{col_id}`` — see :class:`MongoOp` below.
    """

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NIN = "nin"
    CONTAINS = "c"
    NOT_CONTAINS = "nc"
    REGEX = "r"
    SEARCH = "s"
    NOT_SET = "ns"


class MongoOp:
    """Raw MongoDB operators for the ``/results/{col_id}`` endpoint.

    Background: the results endpoint runs ``conditions`` through a different
    translator (``core/utils/mongo.go:GetMongoQuery``) which only special-cases
    ``eq`` and otherwise emits ``bson.M{c.Op: c.Value}`` — without the ``$``
    prefix Mongo expects. So sending ``op="nin"`` produces an invalid query
    and matches nothing.

    The workaround is to send the operator already ``$``-prefixed
    (``op="$nin"``). The default branch of ``GetMongoQuery`` then produces
    ``bson.M{"$nin": value}``, a valid Mongo expression. Verified empirically
    against Crawlab self-hosted in May 2026.

    This is a hack — if Crawlab fixes the translator (auto-prefixing ``$``),
    these names will need to be updated.
    """

    EQ = "eq"
    NE = "$ne"
    GT = "$gt"
    GTE = "$gte"
    LT = "$lt"
    LTE = "$lte"
    IN = "$in"
    NIN = "$nin"
    REGEX = "$regex"


def eq(key: str, value: object) -> dict[str, object]:
    """Shortcut for the common ``{"key": k, "op": "eq", "value": v}`` shape."""
    return {"key": key, "op": Op.EQ, "value": value}
