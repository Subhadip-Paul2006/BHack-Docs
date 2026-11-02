"""API integration tests against an in-memory SQLite engine: health, case creation, list round-trip."""

from __future__ import annotations

import uuid


async def test_healthz_returns_200(api_client):
    response = await api_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


async def test_create_case_returns_201_with_identifier(api_client):
    response = await api_client.post("/cases", json={"title": "Investigate sample media"})
    assert response.status_code == 201
    body = response.json()
    # The returned identifier must parse as the case_id the route generated.
    case_id = uuid.UUID(body["case_id"])
    assert isinstance(case_id, uuid.UUID)
    assert body["title"] == "Investigate sample media"
    assert body["status"] == "CREATED"


async def test_list_cases_contains_created_case(api_client):
    created = await api_client.post("/cases", json={"title": "Round-trip case"})
    assert created.status_code == 201
    case_id = created.json()["case_id"]

    response = await api_client.get("/cases")
    assert response.status_code == 200
    listed = response.json()
    assert any(item["case_id"] == case_id for item in listed)
