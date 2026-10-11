"""Test setup.

Tests that need MongoDB use TEST_MONGO_URI (default mongodb://127.0.0.1:27017) and ALWAYS use a
dedicated throw-away database name, which is dropped at the end. They never touch MONGO_DB.
If MongoDB is unreachable those tests are SKIPPED (reported as skipped, never as passed).
"""
import os
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEST_URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")
TEST_DB = f"hrone_phase1_test_{uuid.uuid4().hex[:8]}"
# main.py reads these lazily on first use, so set them before anything touches the database.
os.environ["MONGO_URI"] = TEST_URI
os.environ["MONGO_DB"] = TEST_DB

import app.main as main  # noqa: E402


def clear_database(db_name: str) -> None:
    """Remove only documents from a throw-away test DB.

    Atlas roles commonly permit collection-level writes but not the cluster-level
    dropDatabase command.  Keeping the empty database and its indexes is safe and
    avoids requiring broader privileges.
    """
    db = main.get_client()[db_name]
    for collection_name in db.list_collection_names():
        db[collection_name].delete_many({})


def _mongo_up() -> bool:
    from pymongo import MongoClient

    try:
        MongoClient(TEST_URI, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:
        return False


MONGO_UP = _mongo_up()
needs_mongo = pytest.mark.skipif(not MONGO_UP, reason="MongoDB not reachable (set TEST_MONGO_URI)")


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:  # runs lifespan -> creates indexes
        yield c
    if MONGO_UP:
        clear_database(TEST_DB)


@pytest.fixture()
def clean(client):
    """Empty both collections (indexes are kept)."""
    main.employees_col().delete_many({})
    main.attendance_col().delete_many({})
    yield


def make_emp(client, code="EMP0001", **kw):
    body = {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com",
            "department": "Engineering", "joined_on": "2026-01-05"}
    body.update(kw)
    return client.post("/employees", json=body)


def ist_ms(y, mo, d, h, mi, s=0, millis=0):
    from datetime import datetime
    return int(datetime(y, mo, d, h, mi, s, millis * 1000, tzinfo=main.IST).timestamp()) * 1000 + millis
