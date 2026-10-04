from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_calculate_post():
    r = client.post("/api/v1/toll/calculate", json={
        "motorway": "M-2", "start": "Lahore", "end": "Islamabad", "vehicle_type": "car"})
    assert r.status_code == 200 and r.json()["total_toll"] == 1430


def test_unknown_station_gives_suggestions():
    r = client.post("/api/v1/toll/calculate", json={
        "motorway": "M-2", "start": "Lahor", "end": "Islamabad", "vehicle_type": "car"})
    assert r.status_code == 404 and "Lahore" in r.json()["suggestions"]


def test_catalogue():
    assert len(client.get("/api/v1/motorways").json()) >= 8
    assert client.get("/api/v1/motorways/M-5/stations").status_code == 200


def test_admin_needs_key():
    assert client.post("/api/v1/admin/reload").status_code == 401