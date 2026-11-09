"""Tests for the local product catalog endpoints.

These exercise only the /products* routes and never touch PayPal (no network,
no credentials). The catalog is static JSON, so assertions are deterministic.
"""

from starlette.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_list_products():
    """GET /products returns the whole catalog as a non-empty list."""
    resp = client.get("/products")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 30
    # Every item exposes the common required fields.
    first = body[0]
    for field in ("id", "name", "category", "brand", "price_inr", "rating", "description"):
        assert field in first


def test_get_product():
    """GET /products/{id} returns the matching product."""
    resp = client.get("/products/LAP001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "LAP001"
    assert body["category"] == "laptops"
    # Laptop-specific fields are present.
    assert body["processor"] is not None
    assert body["gpu"] is not None


def test_get_product_not_found():
    """GET /products/{id} with an unknown id returns 404."""
    resp = client.get("/products/DOES-NOT-EXIST")
    assert resp.status_code == 404
    assert "No product found" in resp.json()["detail"]


def test_filter_by_category():
    """GET /products/search?category=... returns only that category."""
    resp = client.get("/products/search", params={"category": "laptops"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) > 0
    assert all(p["category"] == "laptops" for p in body)


def test_filter_by_max_price():
    """GET /products/search?max_price=... caps the returned prices."""
    resp = client.get("/products/search", params={"max_price": 20000})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) > 0
    assert all(p["price_inr"] <= 20000 for p in body)
