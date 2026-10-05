"""Domain logic: immutable courses and effectively-once delivery accumulation.

All mutating operations run inside a single ``BEGIN IMMEDIATE``
transaction, which (together with SQLite WAL) gives us:

* serializable commit order for concurrent deliveries;
* atomic all-or-nothing application of a multi-channel submission;
* durability via the persisted database file across container restarts.

Idempotency is keyed on ``(course_id, delivery_id)``: the first accepted
submission stores its exact result, and any byte-identical retry replays
that stored result without touching totals. A reused id with different
content, a stale ``expectedRevision``, or a total that would exceed the
prescription makes the whole request a zero-write failure.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Tuple

from . import fixedpoint
from .db import connect, transaction
from .schemas import CreateCourseRequest, DeliveryRequest


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _canonical_increments(payload: DeliveryRequest) -> Dict[str, int]:
    return {ch: fixedpoint.parse_dose(v) for ch, v in payload.increments.items()}


def _course_row(conn: sqlite3.Connection, course_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM courses WHERE id = ?", (course_id,)
    ).fetchone()


def _totals(conn: sqlite3.Connection, course_id: str) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT channel, prescribed_u, delivered_u FROM channel_state "
        "WHERE course_id = ? ORDER BY rowid",
        (course_id,),
    ).fetchall()
    return {
        r["channel"]: {
            "prescribed": fixedpoint.to_canonical(r["prescribed_u"]),
            "delivered": fixedpoint.to_canonical(r["delivered_u"]),
        }
        for r in rows
    }


def _serialize_course(row: sqlite3.Row, channels: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "courseId": row["id"],
        "revision": row["revision"],
        "status": row["status"],
        "channels": [
            {
                "name": name,
                "prescribed": info["prescribed"],
                "delivered": info["delivered"],
            }
            for name, info in channels.items()
        ],
        "totals": {name: info["delivered"] for name, info in channels.items()},
    }


def get_course(course_id: str, conn: sqlite3.Connection | None = None) -> Dict[str, Any]:
    own = conn is None
    conn = conn or connect()
    try:
        row = _course_row(conn, course_id)
        if row is None:
            raise ApiError(404, "course_not_found", f"course {course_id!r} does not exist")
        return _serialize_course(row, _totals(conn, course_id))
    finally:
        if own:
            conn.close()


def create_course(course_id: str, payload: CreateCourseRequest) -> Tuple[Dict[str, Any], bool]:
    """Create an immutable course.

    Returns ``(body, created)`` where ``created`` is False when an
    identical PUT was replayed. A conflicting re-definition raises 409.
    """
    requested = [
        {"name": c.name, "prescribed_u": fixedpoint.parse_dose(c.prescribed)}
        for c in payload.channels
    ]
    requested_json = json.dumps(
        [
            {"name": c["name"], "prescribed": fixedpoint.to_canonical(c["prescribed_u"])}
            for c in requested
        ],
        separators=(",", ":"),
    )
    conn = connect()
    try:
        for attempt in range(100):
            try:
                with transaction(conn):
                    row = _course_row(conn, course_id)
                    if row is not None:
                        if row["channels_json"] != requested_json:
                            raise ApiError(
                                409,
                                "course_conflict",
                                "course already exists with a different prescription; "
                                "courses are immutable",
                            )
                        # Identical content: replay the original result.
                        body = _serialize_course(row, _totals(conn, course_id))
                        body["replayed"] = True
                        return body, False
                    conn.execute(
                        "INSERT INTO courses (id, revision, status, channels_json, created_at) "
                        "VALUES (?, 0, 'active', ?, ?)",
                        (course_id, requested_json, time.time()),
                    )
                    for c in requested:
                        conn.execute(
                            "INSERT INTO channel_state (course_id, channel, prescribed_u, delivered_u) "
                            "VALUES (?, ?, ?, 0)",
                            (course_id, c["name"], c["prescribed_u"]),
                        )
                    row = _course_row(conn, course_id)
                    body = _serialize_course(row, _totals(conn, course_id))
                    body["replayed"] = False
                    return body, True
            except sqlite3.OperationalError as exc:  # pragma: no cover - contention
                if "locked" in str(exc) and attempt < 99:
                    time.sleep(0.01 * (attempt + 1))
                    continue
                raise
    finally:
        conn.close()
    raise RuntimeError("unreachable")  # pragma: no cover


def submit_delivery(course_id: str, payload: DeliveryRequest) -> Dict[str, Any]:
    increments = _canonical_increments(payload)
    increments_json = json.dumps(
        {ch: fixedpoint.to_canonical(v) for ch, v in increments.items()},
        separators=(",", ":"),
        sort_keys=True,
    )
    conn = connect()
    try:
        for attempt in range(100):
            try:
                return _submit_once(conn, course_id, payload, increments, increments_json)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) and attempt < 99:
                    time.sleep(0.005 * (attempt + 1))
                    continue
                raise
    finally:
        conn.close()
    raise RuntimeError("unreachable")  # pragma: no cover


def _submit_once(
    conn: sqlite3.Connection,
    course_id: str,
    payload: DeliveryRequest,
    increments: Dict[str, int],
    increments_json: str,
) -> Dict[str, Any]:
    with transaction(conn):
        course = _course_row(conn, course_id)
        if course is None:
            raise ApiError(404, "course_not_found", f"course {course_id!r} does not exist")

        existing = conn.execute(
            "SELECT * FROM deliveries WHERE course_id = ? AND id = ?",
            (course_id, payload.deliveryId),
        ).fetchone()
        if existing is not None:
            return _replay_or_conflict(existing, payload, increments_json)

        # New deliveryId: every rejection below writes nothing.
        if course["status"] == "complete":
            raise ApiError(
                409,
                "course_complete",
                "course already received its full prescription; only exact replays "
                "of the original submission are accepted",
            )

        if payload.expectedRevision != course["revision"]:
            raise ApiError(
                409,
                "revision_conflict",
                f"expectedRevision {payload.expectedRevision} does not match current "
                f"revision {course['revision']}",
            )

        state_rows = conn.execute(
            "SELECT channel, prescribed_u, delivered_u FROM channel_state WHERE course_id = ?",
            (course_id,),
        ).fetchall()
        state = {
            r["channel"]: {"prescribed_u": r["prescribed_u"], "delivered_u": r["delivered_u"]}
            for r in state_rows
        }

        unknown = sorted(set(increments) - set(state))
        if unknown:
            raise ApiError(
                422,
                "unknown_channel",
                f"increments reference unknown channels: {', '.join(unknown)}",
            )

        new_totals: Dict[str, int] = {}
        for channel, delta in increments.items():
            total = state[channel]["delivered_u"] + delta
            if total > state[channel]["prescribed_u"]:
                raise ApiError(
                    422,
                    "over_prescription",
                    f"channel {channel!r} would total {fixedpoint.to_canonical(total)} "
                    f"against prescription {fixedpoint.to_canonical(state[channel]['prescribed_u'])}",
                )
            new_totals[channel] = total

        # All checks passed: apply the whole submission atomically.
        for channel, total in new_totals.items():
            conn.execute(
                "UPDATE channel_state SET delivered_u = ? WHERE course_id = ? AND channel = ?",
                (total, course_id, channel),
            )
        new_revision = course["revision"] + 1

        all_channels = list(state.keys())
        final_totals = {
            ch: new_totals.get(ch, state[ch]["delivered_u"]) for ch in all_channels
        }
        complete = all(
            final_totals[ch] == state[ch]["prescribed_u"] for ch in all_channels
        )
        new_status = "complete" if complete else "active"
        conn.execute(
            "UPDATE courses SET revision = ?, status = ? WHERE id = ?",
            (new_revision, new_status, course_id),
        )

        totals_json = json.dumps(
            {ch: fixedpoint.to_canonical(v) for ch, v in final_totals.items()},
            separators=(",", ":"),
            sort_keys=True,
        )
        conn.execute(
            "INSERT INTO deliveries (id, course_id, base_rev, increments_json, "
            "result_rev, result_status, result_totals_json, accepted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                payload.deliveryId,
                course_id,
                payload.expectedRevision,
                increments_json,
                new_revision,
                new_status,
                totals_json,
                time.time(),
            ),
        )

        return {
            "courseId": course_id,
            "deliveryId": payload.deliveryId,
            "revision": new_revision,
            "status": new_status,
            "replayed": False,
            "totals": {ch: fixedpoint.to_canonical(v) for ch, v in final_totals.items()},
        }


def _replay_or_conflict(
    existing: sqlite3.Row, payload: DeliveryRequest, increments_json: str
) -> Dict[str, Any]:
    same_content = (
        existing["increments_json"] == increments_json
        and existing["base_rev"] == payload.expectedRevision
    )
    if not same_content:
        raise ApiError(
            409,
            "delivery_id_conflict",
            f"deliveryId {payload.deliveryId!r} was already submitted with different "
            "content; the original submission cannot be changed",
        )
    # Exact replay of the first submission -> return its original result.
    return {
        "courseId": existing["course_id"],
        "deliveryId": existing["id"],
        "revision": existing["result_rev"],
        "status": existing["result_status"],
        "replayed": True,
        "totals": json.loads(existing["result_totals_json"]),
    }
