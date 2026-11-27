import time

import pytest

from app import initdata
from tests.conftest import BUYER, TOKEN, make_init_data


def test_valid_init_data_returns_user():
    assert initdata.validate(make_init_data(), TOKEN)["id"] == BUYER


def test_tampered_init_data_is_rejected():
    data = make_init_data().replace("Test", "Evil")
    with pytest.raises(initdata.InvalidInitData):
        initdata.validate(data, TOKEN)


def test_other_bot_signature_is_rejected():
    with pytest.raises(initdata.InvalidInitData):
        initdata.validate(make_init_data(token="999:other"), TOKEN)


def test_expired_init_data_is_rejected():
    old = make_init_data(auth_date=time.time() - 90000)
    with pytest.raises(initdata.InvalidInitData):
        initdata.validate(old, TOKEN, max_age=86400)


@pytest.mark.parametrize("value", ["", "hash=abc", "not a query"])
def test_garbage_is_rejected(value):
    with pytest.raises(initdata.InvalidInitData):
        initdata.validate(value, TOKEN)


def test_http_client_never_logs_request_urls():
    import logging
    import app.telegram  # noqa: F401 - importing sets the levels
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
