import json

import pytest

from app.config import ConfigError, load_products
from tests.conftest import make_settings


def write(tmp_path, items):
    path = tmp_path / "products.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


GOOD = {"id": "guide", "title": "Rehber", "description": "PDF", "stars": 10, "content": "https://x.y/z"}


def test_example_catalog_loads():
    products = load_products("products.example.json")
    assert products and all(p.stars > 0 for p in products.values())


def test_valid_product(tmp_path):
    assert load_products(write(tmp_path, [GOOD]))["guide"].kind == "one_time"


@pytest.mark.parametrize("change", [
    {"id": "Bad Id"}, {"title": ""}, {"title": "x" * 33}, {"stars": 0}, {"stars": 1.5}, {"stars": True},
    {"content": ""}, {"kind": "lifetime"}, {"photo_url": "http://insecure"},
    {"category": "x" * 25}, {"badge": "x" * 17}, {"badge": 5},
])
def test_invalid_products(tmp_path, change):
    with pytest.raises(ConfigError):
        load_products(write(tmp_path, [{**GOOD, **change}]))


def test_duplicate_ids(tmp_path):
    with pytest.raises(ConfigError):
        load_products(write(tmp_path, [GOOD, GOOD]))


def test_settings_require_support_and_terms_when_selling():
    with pytest.raises(ConfigError):
        make_settings(support_text="").check()
    make_settings(support_text="", sales_enabled=False).check()


@pytest.mark.parametrize("change", [{"webhook_secret": "short"}, {"webapp_url": "http://plain"}])
def test_settings_reject_weak_values(change):
    with pytest.raises(ConfigError):
        make_settings(**change).check()


def test_category_and_badge_are_optional_and_public(tmp_path):
    product = load_products(write(tmp_path, [GOOD | {"category": " Rehberler ", "badge": ""}]))["guide"]
    assert product.category == "Rehberler" and product.badge is None
    assert product.public()["category"] == "Rehberler" and "content" not in product.public()


@pytest.mark.parametrize("change", [{"access_days": 0}, {"access_days": 4000}, {"access_days": "x"},
                                    {"access_days": 30, "kind": "subscription"}])
def test_access_days_rules(tmp_path, change):
    with pytest.raises(ConfigError):
        load_products(write(tmp_path, [GOOD | change]))


def test_access_days_accepts_numbers_and_strings(tmp_path):
    assert load_products(write(tmp_path, [GOOD | {"access_days": "90"}]))["guide"].access_days == 90
