import httpx
import pytest

from app.config import Settings
from app.errors import UpstreamError
from app.gateway import KeitaroGateway


def live_settings(**kwargs):
    return Settings(
        _env_file=None, app_mode="live", keitaro_url="https://tracker.test", keitaro_api_key="test-secret-key", **kwargs
    )


def test_admin_api_base_path_key_header_and_partial_put():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 12})

    gateway = KeitaroGateway(live_settings())
    gateway.client.close()
    gateway.client = httpx.Client(
        base_url="https://tracker.test/admin_api/v1/",
        headers={"Api-Key": "test-secret-key"},
        transport=httpx.MockTransport(handle),
    )
    try:
        gateway.update_stream(12, {"offers": [{"offer_id": 101, "share": 100, "state": "active"}]})
        assert str(requests[0].url) == "https://tracker.test/admin_api/v1/streams/12"
        assert requests[0].headers["Api-Key"] == "test-secret-key"
        assert b"filters" not in requests[0].content
    finally:
        gateway.close()


@pytest.mark.parametrize("status", [301, 401, 403, 500])
def test_errors_do_not_expose_upstream_body_or_key(status):
    gateway = KeitaroGateway(live_settings())
    gateway.client.close()
    gateway.client = httpx.Client(
        base_url="https://tracker.test/",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="test-secret-key")),
    )
    try:
        with pytest.raises(UpstreamError) as error:
            gateway.offers()
        assert "test-secret-key" not in str(error.value)
    finally:
        gateway.close()


def test_live_configuration_requires_key():
    with pytest.raises(ValueError):
        Settings(_env_file=None, app_mode="live", keitaro_url="https://tracker.test", keitaro_api_key="")


def test_example_environment_starts_in_demo_mode():
    settings = Settings(_env_file=".env.example")
    assert settings.app_mode == "demo"
    assert settings.default_domain_id is None
