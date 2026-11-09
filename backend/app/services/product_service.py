"""Local product catalog service.

Loads a deterministic product catalog from data/products.json and exposes
read-only helpers to list, fetch, and filter products. There is no database
and no external call — the JSON file is the single source of truth, so results
are fully deterministic for testing and for a future Claude tool.

This module is intentionally independent of the PayPal integration.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("paypilot.products")

# data/products.json lives at the backend root: this file is
# backend/app/services/product_service.py, so parents[2] == backend/.
_DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "products.json"


class Product(BaseModel):
    """A single catalog product.

    The first block of fields is common to every category; the rest are
    category-specific and therefore optional (a laptop has a gpu, a monitor a
    refresh rate, and so on). Keeping them optional on one model lets a single
    response schema serve every category without overengineering.
    """

    # Common fields (present for every product).
    id: str
    name: str
    category: str
    brand: str
    price_inr: int
    rating: float
    description: str

    # Laptop / smartphone / tablet specs.
    ram_gb: Optional[int] = None
    storage_gb: Optional[int] = None
    processor: Optional[str] = None
    gpu: Optional[str] = None
    screen_size_in: Optional[float] = None
    battery_mah: Optional[int] = None
    camera_mp: Optional[int] = None

    # Headphone specs.
    wireless: Optional[bool] = None
    noise_cancelling: Optional[bool] = None
    battery_hours: Optional[int] = None
    form_factor: Optional[str] = None

    # Monitor specs.
    resolution: Optional[str] = None
    refresh_rate_hz: Optional[int] = None
    panel_type: Optional[str] = None


class ProductNotFoundError(Exception):
    """Raised when a product id does not exist in the catalog."""


def _load_catalog() -> list[Product]:
    """Read and validate the catalog from disk (called once at import)."""
    with _DATA_FILE.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    products = [Product(**item) for item in raw]
    logger.info("Loaded %d products from %s", len(products), _DATA_FILE.name)
    return products


# Loaded once at import time. The catalog is static test data, so there is no
# need to re-read the file per request.
_CATALOG: list[Product] = _load_catalog()


def list_products() -> list[Product]:
    """Return all products in catalog order."""
    return list(_CATALOG)


def get_product(product_id: str) -> Product:
    """Return a single product by id (case-insensitive).

    Raises:
        ProductNotFoundError: if no product has that id.
    """
    key = product_id.strip().lower()
    for product in _CATALOG:
        if product.id.lower() == key:
            return product
    raise ProductNotFoundError(f"No product found with id {product_id}")


def search_products(
    category: Optional[str] = None,
    max_price: Optional[int] = None,
    min_rating: Optional[float] = None,
    keyword: Optional[str] = None,
) -> list[Product]:
    """Filter the catalog by any combination of criteria.

    All filters are optional and combined with AND. With no filters this
    returns the full catalog (same as list_products).

    Args:
        category: Exact category match (case-insensitive), e.g. "laptops".
        max_price: Keep products with price_inr <= this value.
        min_rating: Keep products with rating >= this value.
        keyword: Case-insensitive substring match against name, brand,
            and description.
    """
    results = _CATALOG

    if category is not None:
        cat = category.strip().lower()
        results = [p for p in results if p.category.lower() == cat]

    if max_price is not None:
        results = [p for p in results if p.price_inr <= max_price]

    if min_rating is not None:
        results = [p for p in results if p.rating >= min_rating]

    if keyword is not None and keyword.strip():
        term = keyword.strip().lower()
        results = [
            p
            for p in results
            if term in p.name.lower()
            or term in p.brand.lower()
            or term in p.description.lower()
        ]

    return list(results)
