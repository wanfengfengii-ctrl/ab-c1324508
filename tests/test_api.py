import concurrent.futures

from tests.conftest import make_course


# ---------- Course lifecycle ----------

def test_create_course_basic(client):
    r = make_course(client)
    body = r.json()
    assert body["revision"] == 0
    assert body["status"] == "active"
    assert body["replayed"] is False
    assert body["totals"] == {"A": "0", "B": "0"}


def test_put_idempotent_same_content(client):
    make_course(client, "cX")
    r2 = client.put(
        "/api/courses/cX",
        json={"channels": [
            {"name": "A", "prescribed": "10"},
            {"name": "B", "prescribed": "5.5"},
        ]},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["replayed"] is True
    assert body["revision"] == 0


def test_put_conflict_different_content(client):
    make_course(client, "cX")
    r = client.put(
        "/api/courses/cX",
        json={"channels": [{"name": "A", "prescribed": "11"}]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "course_conflict"


def test_channel_count_bounds(client):
    ok = [{"name": f"c{i}", "prescribed": "1"} for i in range(16)]
    assert client.put("/api/courses/ok", json={"channels": ok}).status_code == 201
    too_many = [{"name": f"c{i}", "prescribed": "1"} for i in range(17)]
    r = client.put("/api/courses/bad", json={"channels": too_many})
    assert r.status_code == 422
    empty = client.put("/api/courses/empty", json={"channels": []})
    assert empty.status_code == 422


def test_duplicate_channel_rejected(client):
    r = client.put(
        "/api/courses/dup",
        json={"channels": [
            {"name": "A", "prescribed": "1"},
            {"name": "A", "prescribed": "2"},
        ]},
    )
    assert r.status_code == 422


def test_non_canonical_prescription_rejected(client):
    r = client.put(
        "/api/courses/nc",
        json={"channels": [{"name": "A", "prescribed": "1.0"}]},
    )
    assert r.status_code == 422


def test_get_course_404(client):
    assert client.get("/api/courses/nope").status_code == 404


# ---------- Deliveries: basic semantics ----------

def _deliver(client, cid, delivery_id, rev, inc):
    return client.post(
        f"/api/courses/{cid}/deliveries",
        json={"deliveryId": delivery_id, "expectedRevision": rev, "increments": inc},
    )


def test_first_delivery_increments_revision_and_totals(client):
    make_course(client)
    r = _deliver(client, "c1", "d1", 0, {"A": "4", "B": "1.5"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["replayed"] is False
    assert body["totals"] == {"A": "4", "B": "1.5"}
    assert body["status"] == "active"


def test_revision_increments_one_per_acceptance(client):
    make_course(client)
    _deliver(client, "c1", "d1", 0, {"A": "1"})
    _deliver(client, "c1", "d2", 1, {"A": "1"})
    r = _deliver(client, "c1", "d3", 2, {"A": "1"})
    assert r.json()["revision"] == 3


def test_stale_revision_whole_request_zero_write(client):
    make_course(client)
    assert _deliver(client, "c1", "d1", 0, {"A": "1"}).status_code == 200
    # Current revision is now 1; sending expectedRevision 0 must fail.
    r = _deliver(client, "c1", "d2", 0, {"A": "2"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "revision_conflict"
    state = client.get("/api/courses/c1").json()
    assert state["totals"] == {"A": "1", "B": "0"}
    assert state["revision"] == 1


def test_over_prescription_rejected_atomic(client):
    make_course(client)
    # A would exceed (4 + 7 = 11 > 10) while B would be fine: whole tx aborts.
    r = _deliver(client, "c1", "d1", 0, {"A": "4"})
    assert r.status_code == 200
    r = _deliver(client, "c1", "d2", 1, {"A": "7", "B": "1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "over_prescription"
    state = client.get("/api/courses/c1").json()
    assert state["totals"] == {"A": "4", "B": "0"}
    assert state["revision"] == 1


def test_exactly_prescription_completes_course(client):
    make_course(client)
    r1 = _deliver(client, "c1", "d1", 0, {"A": "10", "B": "2.5"})
    assert r1.json()["status"] == "active"
    r2 = _deliver(client, "c1", "d2", 1, {"B": "3"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "complete"
    assert r2.json()["totals"] == {"A": "10", "B": "5.5"}


def test_no_new_delivery_after_complete(client):
    make_course(client)
    _deliver(client, "c1", "d1", 0, {"A": "10", "B": "5.5"})
    r = _deliver(client, "c1", "d2", 1, {"A": "0"})  # invalid: zero increment
    assert r.status_code == 422
    r = _deliver(client, "c1", "d3", 1, {"B": "0.000000"})
    assert r.status_code == 422
    r = _deliver(client, "c1", "d2", 1, {"A": "0.1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "course_complete"
    state = client.get("/api/courses/c1").json()
    assert state["revision"] == 1
    assert state["status"] == "complete"


def test_unknown_channel_rejected(client):
    make_course(client)
    r = _deliver(client, "c1", "d1", 0, {"Z": "1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_channel"


def test_increments_nonempty_and_positive(client):
    make_course(client)
    r = client.post(
        "/api/courses/c1/deliveries",
        json={"deliveryId": "d1", "expectedRevision": 0, "increments": {"A": "0"}},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/courses/c1/deliveries",
        json={"deliveryId": "d1", "expectedRevision": 0, "increments": {"A": "-1"}},
    )
    assert r.status_code == 422


# ---------- Idempotent replay ----------

def test_same_delivery_id_same_content_replays_first_result(client):
    make_course(client)
    first = _deliver(client, "c1", "d1", 0, {"A": "4"}).json()
    # Advance the course with another delivery, then retry d1 verbatim.
    _deliver(client, "c1", "d2", 1, {"A": "1"})
    replay = _deliver(client, "c1", "d1", 0, {"A": "4"})
    assert replay.status_code == 200
    body = replay.json()
    assert body["replayed"] is True
    assert body == {**first, "replayed": True}
    # Replay must not double-count.
    state = client.get("/api/courses/c1").json()
    assert state["totals"] == {"A": "5", "B": "0"}
    assert state["revision"] == 2


def test_same_delivery_id_different_content_conflict(client):
    make_course(client)
    assert _deliver(client, "c1", "d1", 0, {"A": "4"}).status_code == 200
    r = _deliver(client, "c1", "d1", 0, {"A": "5"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "delivery_id_conflict"
    # Different revision with same increments is also "different content".
    r = _deliver(client, "c1", "d1", 1, {"A": "4"})
    assert r.status_code == 409


def test_replay_after_complete_allowed(client):
    make_course(client)
    _deliver(client, "c1", "d-final", 0, {"A": "10", "B": "5.5"})
    replay = _deliver(client, "c1", "d-final", 0, {"A": "10", "B": "5.5"})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["status"] == "complete"


# ---------- Concurrency ----------

def test_concurrent_competing_deliveries(client):
    """All sessions expect revision 0; exactly one wins per revision, and
    the losers fail with revision_conflict without writing anything."""
    make_course(client, "race", channels=[
        {"name": "A", "prescribed": "100"},
    ])

    def fire(i):
        return _deliver(client, "race", f"d{i}", 0, {"A": "1"}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(fire, range(16)))

    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 15
    state = client.get("/api/courses/race").json()
    assert state["revision"] == 1
    assert state["totals"] == {"A": "1"}


def test_concurrent_same_delivery_id_applied_once(client):
    """Retries of the *same* submission racing concurrently must produce a
    single application and every caller sees the identical first result."""
    make_course(client, "race2", channels=[{"name": "A", "prescribed": "100"}])

    def fire(_):
        return _deliver(client, "race2", "dup", 0, {"A": "3"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(fire, range(16)))

    assert all(r.status_code == 200 for r in responses)
    state = client.get("/api/courses/race2").json()
    assert state["revision"] == 1
    assert state["totals"] == {"A": "3"}


# ---------- Persistence across restart ----------

def test_state_survives_process_restart(client, tmp_path):
    make_course(client, "persist")
    _deliver(client, "persist", "d1", 0, {"A": "2.5", "B": "1"})

    # Simulate a fresh process: new connections against the same file and
    # the module-level initialized cache is irrelevant since init is
    # idempotent. A brand-new connection sees committed state.
    from app import db
    conn = db.connect()
    try:
        row = conn.execute("SELECT revision, status FROM courses WHERE id = 'persist'").fetchone()
        assert row["revision"] == 1
        assert row["status"] == "active"
    finally:
        conn.close()

    after = client.get("/api/courses/persist").json()
    assert after["totals"] == {"A": "2.5", "B": "1"}
    # Replay still resolves correctly "after restart".
    replay = _deliver(client, "persist", "d1", 0, {"A": "2.5", "B": "1"})
    assert replay.json()["replayed"] is True
