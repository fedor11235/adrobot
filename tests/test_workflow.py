from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.errors import UpstreamError
from app.main import create_app


def stream(client, sid=12):
    return next(s for s in client.get("/api/campaigns/1").json()["streams"] if s["id"] == sid)


def mutate(client, action, extra=None, method="post", sid=12, expected=200):
    revision = stream(client, sid)["revision"]
    response = getattr(client, method)(f"/api/streams/{sid}/{action}", json={"revision": revision, **(extra or {})})
    assert response.status_code == expected, response.text
    return response.json()


def by_id(data, oid):
    return next(o for o in data["offers"] if o["offer_id"] == oid)


def test_create_two_ordered_streams_and_metadata(client, gateway):
    response = client.post("/api/campaigns", json={"name": "test_ivanov_пример", "geo": "au, MX au", "offer_id": 104})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["metadata"] == {"domain_id": 1, "group_id": "1", "traffic_source_id": 1, "state": "active"}
    first, second = gateway.streams(data["id"])
    assert first["filters"] == [{"name": "country", "mode": "accept", "payload": ["AU", "MX"]}]
    assert first["action_payload"] == "https://www.google.com/"
    assert first["position"] < second["position"]
    assert second["offers"] == [{"offer_id": 104, "share": 100, "state": "active"}]


def test_readonly_stream_and_initial_distribution(client, loaded):
    first, second = loaded["streams"]
    assert not first["editable"]
    assert second["total"] == 100 and not second["dirty"]
    mutate(client, "offers", {"offer_id": 104}, sid=11, expected=422)


def test_add_is_local_then_publish(client, gateway, loaded):
    data = mutate(client, "offers", {"offer_id": 104})
    assert data["dirty"] and [o["share"] for o in data["offers"]] == [25] * 4
    assert len(gateway.stream(12)["offers"]) == 3
    published = mutate(client, "publish")
    assert not published["dirty"]
    assert len(gateway.stream(12)["offers"]) == 4


def test_cancel_discards_new_binding_and_restores_weights(client, loaded):
    mutate(client, "offers", {"offer_id": 104})
    cancelled = mutate(client, "cancel")
    assert cancelled["offers"] == loaded["streams"][1]["offers"]
    assert not cancelled["dirty"]


def test_remove_archive_restore_and_cancel(client, gateway, loaded):
    removed = mutate(client, "offers/101/remove")
    assert by_id(removed, 101)["removed"] and by_id(removed, 101)["share"] == 0
    assert [by_id(removed, oid)["share"] for oid in [102, 103]] == [50, 50]
    mutate(client, "publish")
    assert 101 not in [o["offer_id"] for o in gateway.stream(12)["offers"]]
    restored = mutate(client, "offers/101/restore")
    assert not by_id(restored, 101)["removed"] and restored["total"] == 100
    assert by_id(mutate(client, "cancel"), 101)["removed"]
    mutate(client, "offers/101/restore")
    mutate(client, "publish")
    assert 101 in [o["offer_id"] for o in gateway.stream(12)["offers"]]


def test_pinned_weights_survive_add_remove_and_publish(client, gateway, loaded):
    mutate(client, "offers/101", {"pinned": True, "share": 25}, method="patch")
    data = mutate(client, "offers", {"offer_id": 104})
    assert by_id(data, 101)["share"] == 25
    assert [o["share"] for o in data["offers"]] == [25, 25, 25, 25]
    data = mutate(client, "offers/104/remove")
    assert [by_id(data, oid)["share"] for oid in [101, 102, 103]] == [25, 38, 37]
    mutate(client, "publish")
    client.post("/api/campaigns/1/fetch")
    assert by_id(stream(client), 101)["pinned"]
    assert gateway.stream(12)["offers"][0]["share"] == 25


def test_invalid_pins_roll_back_entire_mutation(client, loaded):
    mutate(client, "offers/101", {"pinned": True, "share": 80}, method="patch")
    before = stream(client)
    mutate(client, "offers/102", {"pinned": True, "share": 30}, method="patch", expected=422)
    assert stream(client) == before


def test_all_pinned_must_total_hundred(client, loaded):
    mutate(client, "offers/101", {"pinned": True, "share": 34}, method="patch")
    mutate(client, "offers/102", {"pinned": True, "share": 33}, method="patch")
    mutate(client, "offers/103", {"pinned": True, "share": 32}, method="patch", expected=422)
    mutate(client, "offers/103", {"pinned": True, "share": 33}, method="patch")


def test_fetch_marks_external_deletion_as_archived(client, gateway, loaded):
    gateway.update_stream(12, {"offers": [{"offer_id": 102, "share": 50}, {"offer_id": 103, "share": 50}]})
    response = client.post("/api/campaigns/1/fetch")
    assert response.status_code == 200
    data = stream(client)
    assert by_id(data, 101)["removed"] and not data["dirty"]
    assert data["total"] == 100


def test_fetch_preserves_unchanged_remote_local_draft(client, loaded):
    before = mutate(client, "offers", {"offer_id": 104})
    assert client.post("/api/campaigns/1/fetch").status_code == 200
    after = stream(client)
    assert after["offers"] == before["offers"] and after["dirty"]


@pytest.mark.parametrize("operation", ["fetch", "publish"])
def test_remote_conflict_never_overwrites_either_side(client, gateway, loaded, operation):
    before = mutate(client, "offers", {"offer_id": 104})
    external = [{"offer_id": 101, "share": 100}]
    gateway.update_stream(12, {"offers": external})
    if operation == "fetch":
        response = client.post("/api/campaigns/1/fetch")
        assert response.status_code == 409
    else:
        mutate(client, "publish", expected=409)
    assert stream(client)["offers"] == before["offers"]
    assert gateway.stream(12)["offers"] == external
    mutate(client, "cancel")
    assert client.post("/api/campaigns/1/fetch").status_code == 200


def test_stale_browser_revision_rejected(client, loaded):
    stale = stream(client)["revision"]
    mutate(client, "offers", {"offer_id": 104})
    response = client.post("/api/streams/12/offers/101/remove", json={"revision": stale})
    assert response.status_code == 409
    assert not by_id(stream(client), 101)["removed"]


def test_publish_preserves_unrelated_stream_fields(client, gateway, loaded):
    gateway.update_stream(
        12, {"filters": [{"name": "bot", "mode": "reject", "payload": ["1"]}], "comments": "Чужая правка"}
    )
    mutate(client, "offers", {"offer_id": 104})
    mutate(client, "publish")
    assert gateway.stream(12)["comments"] == "Чужая правка"
    assert gateway.stream(12)["filters"][0]["name"] == "bot"


def test_timeout_after_remote_success_can_be_retried(client, gateway, loaded, monkeypatch):
    mutate(client, "offers", {"offer_id": 104})
    original = gateway.update_stream

    def lost_response(sid, data):
        original(sid, data)
        raise UpstreamError("Тестовый обрыв соединения")

    monkeypatch.setattr(gateway, "update_stream", lost_response)
    mutate(client, "publish", expected=502)
    assert stream(client)["dirty"] and len(gateway.stream(12)["offers"]) == 4
    monkeypatch.setattr(gateway, "update_stream", original)
    assert not mutate(client, "publish")["dirty"]


def test_empty_draft_allowed_empty_publication_rejected(client, gateway, loaded):
    for oid in [101, 102, 103]:
        mutate(client, f"offers/{oid}/remove")
    mutate(client, "publish", expected=422)
    assert len(gateway.stream(12)["offers"]) == 3


def test_duplicate_offer_rejected(client, loaded):
    mutate(client, "offers", {"offer_id": 101}, expected=409)


@pytest.mark.parametrize(
    "patch", [{"geo": "AUSTRALIA"}, {"offer_id": True}, {"offer_id": -1}, {"name": " "}, {"unknown": 1}]
)
def test_input_validation(client, patch):
    data = {"name": "Тест", "geo": "AU", "offer_id": 101, **patch}
    assert client.post("/api/campaigns", json=data).status_code == 422


def test_failed_creation_archives_partial_campaign(client, gateway, monkeypatch):
    calls = 0
    original = gateway.create_stream

    def fail_second(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise UpstreamError("Тестовая ошибка")
        return original(data)

    monkeypatch.setattr(gateway, "create_stream", fail_second)
    response = client.post("/api/campaigns", json={"name": "Незавершённая", "geo": "AU", "offer_id": 101})
    assert response.status_code == 502
    assert gateway.campaign(2)["state"] == "deleted"
    assert [c["id"] for c in gateway.campaigns()] == [1]


def test_persistence_across_app_restart(client, gateway, settings, loaded):
    expected = mutate(client, "offers", {"offer_id": 104})
    with TestClient(create_app(settings, gateway)) as other:
        actual = stream(other)
        assert actual["offers"] == expected["offers"] and actual["dirty"]


def test_removed_remote_stream_preserves_local_history(client, gateway, loaded):
    gateway.update_stream(12, {"state": "deleted"})
    assert client.post("/api/campaigns/1/fetch").status_code == 200
    assert stream(client)["remote_missing"]
    mutate(client, "offers/101/remove", expected=409)


def test_demo_external_edit_route(client, loaded):
    response = client.put("/api/demo/streams/12/offers", json={"offers": [{"offer_id": 101, "share": 100}]})
    assert response.status_code == 200
    client.post("/api/campaigns/1/fetch")
    assert by_id(stream(client), 102)["removed"]


def test_disabled_offer_state_not_enabled_by_publish(client, gateway, loaded):
    rows = deepcopy(gateway.stream(12)["offers"])
    rows[0].update(state="disabled", share=0)
    rows[1]["share"], rows[2]["share"] = 50, 50
    gateway.update_stream(12, {"offers": rows})
    client.post("/api/campaigns/1/fetch")
    mutate(client, "offers", {"offer_id": 104})
    mutate(client, "publish")
    assert next(o for o in gateway.stream(12)["offers"] if o["offer_id"] == 101)["state"] == "disabled"


def test_optional_http_basic(client, gateway, settings):
    settings = settings.model_copy(update={"app_password": __import__("pydantic").SecretStr("test-password")})
    with TestClient(create_app(settings, gateway)) as protected:
        assert protected.get("/api/campaigns").status_code == 401
        assert protected.get("/api/campaigns", auth=("adrobot", "test-password")).status_code == 200


def test_search_by_id_and_name(client):
    assert client.get("/api/offers?q=104").json()[0]["id"] == 104
    assert client.get("/api/offers?q=МАЛИНА").json() == []  # Название латиницей.
    assert len(client.get("/api/offers?q=malina").json()) == 2
