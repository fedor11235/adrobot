from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient

from alembic import command
from app.config import Settings
from app.gateway import DemoGateway
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        app_mode="demo",
        database_url=f"sqlite:///{tmp_path}/app.db",
        demo_database_url=f"sqlite:///{tmp_path}/remote.db",
        app_password="",
    )


@pytest.fixture
def gateway(settings):
    gateway = DemoGateway(settings)
    yield gateway
    gateway.close()


@pytest.fixture
def migrated(settings):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")


@pytest.fixture
def client(settings, gateway, migrated):
    with TestClient(create_app(settings, gateway)) as client:
        yield client


@pytest.fixture
def loaded(client):
    response = client.post("/api/campaigns/1/fetch")
    assert response.status_code == 200, response.text
    return response.json()
