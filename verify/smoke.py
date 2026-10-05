#!/usr/bin/env python3
"""End-to-end HTTP smoke test for the dose-control API.

It is deliberately written with the standard library only so the tiny
``verify`` container needs no extra tooling and so it can drive the real
HTTP server (never in-process calls) — that is what makes it a genuine
smoke test after a container restart.

Stages
------
* ``smoke1``  — health, create course, concurrent contention, idempotent
                replay, conflicting reuse, over-prescription zero-write,
                completion. State is written to ``STATE_FILE``.
* ``smoke2``  — runs after the API process has been restarted: proves the
                revision, totals and ``complete`` status survived, and
                that the original submission still replays.
* ``all``     — both stages back to back (used when something external
                restarts the peer, or locally where this script manages a
                uvicorn subprocess itself).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
STATE_FILE = os.environ.get("VERIFY_STATE", "/tmp/verify-state.json")
STAGE = os.environ.get("STAGE", "all")


class SmokeFailure(AssertionError):
    pass


def _request(method: str, path: str, body: dict | None = None, *, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, payload = resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode())
    if expect is not None and status != expect:
        raise SmokeFailure(f"{method} {path}: expected {expect}, got {status} {payload}")
    return status, payload


def wait_healthy(timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - any failure means retry
            last = exc
        time.sleep(0.5)
    raise SmokeFailure(f"API did not become healthy within {timeout_s}s ({last})")


def stage_smoke1() -> dict:
    wait_healthy()
    course_id = "verify-" + uuid.uuid4().hex[:12]
    channels = [
        {"name": "A", "prescribed": "1"},
        {"name": "B", "prescribed": "0.5"},
    ]
    status, course = _request("PUT", f"/api/courses/{course_id}",
                              {"channels": channels}, expect=201)
    assert course["revision"] == 0 and course["status"] == "active", course

    # Retrying the identical PUT replays the existing result.
    status, replay_put = _request("PUT", f"/api/courses/{course_id}",
                                  {"channels": channels}, expect=200)
    assert replay_put["replayed"] is True, replay_put
    # A different prescription on the same id is a conflict.
    status, conflict = _request(
        "PUT", f"/api/courses/{course_id}",
        {"channels": [{"name": "A", "prescribed": "2"}]}, expect=409)
    assert conflict["error"]["code"] == "course_conflict", conflict

    # --- Concurrent contention: 12 sessions, all expecting revision 0.
    # Exactly one must be accepted; the rest are stale-revision failures
    # and must not have written anything.
    def fire(i: int):
        return _request(
            "POST", f"/api/courses/{course_id}/deliveries",
            {"deliveryId": f"race-{i}", "expectedRevision": 0,
             "increments": {"A": "0.1"}},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(fire, range(12)))
    accepted = [r for s, r in results if s == 200]
    rejected = [r for s, r in results if s == 409]
    if len(accepted) != 1 or len(rejected) != 11:
        raise SmokeFailure(
            f"expected exactly 1 acceptance / 11 revision conflicts, "
            f"got {len(accepted)} / {len(rejected)}: {results}")
    winner = accepted[0]
    assert winner["revision"] == 1, winner
    assert winner["totals"] == {"A": "0.1", "B": "0"}, winner
    winner_id = winner["deliveryId"]

    status, state = _request("GET", f"/api/courses/{course_id}", expect=200)
    assert state["revision"] == 1 and state["totals"] == {"A": "0.1", "B": "0"}, state

    # --- Exact replay of the winning submission returns its first result
    # and never double-counts, even though the revision has moved on.
    status, replay = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": winner_id, "expectedRevision": 0,
         "increments": {"A": "0.1"}}, expect=200)
    assert replay["replayed"] is True and replay["revision"] == 1, replay

    # Same deliveryId with different content is a permanent conflict.
    status, dup = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": winner_id, "expectedRevision": 0,
         "increments": {"A": "0.2"}}, expect=409)
    assert dup["error"]["code"] == "delivery_id_conflict", dup

    # --- Over-prescription is an all-or-nothing failure.
    status, over = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": "over-1", "expectedRevision": 1,
         "increments": {"A": "1", "B": "0.1"}}, expect=422)
    assert over["error"]["code"] == "over_prescription", over
    status, state = _request("GET", f"/api/courses/{course_id}", expect=200)
    assert state["revision"] == 1 and state["status"] == "active", state
    assert state["totals"] == {"A": "0.1", "B": "0"}, state

    # Stale revision is likewise a zero-write failure.
    status, stale = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": "stale-1", "expectedRevision": 0,
         "increments": {"A": "0.1"}}, expect=409)
    assert stale["error"]["code"] == "revision_conflict", stale

    # --- Complete the prescription across both channels.
    status, r2 = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": "fill-A", "expectedRevision": 1,
         "increments": {"A": "0.9"}}, expect=200)
    assert r2["revision"] == 2 and r2["status"] == "active", r2
    status, r3 = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": "fill-B", "expectedRevision": 2,
         "increments": {"B": "0.5"}}, expect=200)
    assert r3["revision"] == 3 and r3["status"] == "complete", r3
    assert r3["totals"] == {"A": "1", "B": "0.5"}, r3

    # After completion only replays of the original submissions work.
    status, after = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": "too-late", "expectedRevision": 3,
         "increments": {"A": "0.000001"}}, expect=409)
    assert after["error"]["code"] == "course_complete", after
    status, replay_final = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": "fill-B", "expectedRevision": 2,
         "increments": {"B": "0.5"}}, expect=200)
    assert replay_final["replayed"] is True, replay_final
    assert replay_final["status"] == "complete", replay_final

    return {"courseId": course_id, "winnerId": winner_id}


def stage_smoke2(saved: dict) -> None:
    wait_healthy()
    course_id = saved["courseId"]
    winner_id = saved["winnerId"]

    status, state = _request("GET", f"/api/courses/{course_id}", expect=200)
    assert state["revision"] == 3, f"revision lost across restart: {state}"
    assert state["status"] == "complete", f"status lost across restart: {state}"
    assert state["totals"] == {"A": "1", "B": "0.5"}, f"totals lost: {state}"

    # The idempotency record survived as well: a new submission is still
    # refused, and the original winning submission still replays.
    status, after = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": "post-restart", "expectedRevision": 3,
         "increments": {"A": "0.000001"}}, expect=409)
    assert after["error"]["code"] == "course_complete", after
    status, replay = _request(
        "POST", f"/api/courses/{course_id}/deliveries",
        {"deliveryId": winner_id, "expectedRevision": 0,
         "increments": {"A": "0.1"}}, expect=200)
    assert replay["replayed"] is True and replay["revision"] == 1, replay


def main() -> int:
    try:
        if STAGE in ("smoke1", "all"):
            saved = stage_smoke1()
            with open(STATE_FILE, "w") as fh:
                json.dump(saved, fh)
            print(f"[verify] smoke1 OK: {saved}", flush=True)
            if STAGE == "all":
                stage_smoke2(saved)
                print("[verify] smoke2 OK (in-place state check)", flush=True)
        elif STAGE == "smoke2":
            with open(STATE_FILE) as fh:
                saved = json.load(fh)
            stage_smoke2(saved)
            print("[verify] smoke2 OK after restart", flush=True)
        else:
            raise SmokeFailure(f"unknown STAGE={STAGE!r}")
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        print(f"[verify] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
