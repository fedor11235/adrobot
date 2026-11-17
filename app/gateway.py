"""HTTP-клиент официального Admin API и изолированный постоянный демотрекер."""

from copy import deepcopy

import httpx
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db import make_engine, make_sessions, writing
from app.errors import AppError, UpstreamError


class KeitaroGateway:
    def __init__(self, settings):
        base = settings.keitaro_url.rstrip("/")
        if not base.endswith("/admin_api/v1"):
            base += "/admin_api/v1"
        self.client = httpx.Client(
            base_url=base + "/",
            headers={"Api-Key": settings.keitaro_api_key.get_secret_value()},
            timeout=settings.keitaro_timeout,
            follow_redirects=False,
        )

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path.lstrip("/"), **kwargs)
        except httpx.RequestError as exc:
            raise UpstreamError("Keitaro недоступен или не ответил вовремя. Изменения сохранены локально.") from exc
        if not response.is_success:
            # Ответ трекера может содержать URL и ключи — не передаём его в браузер/логи.
            raise UpstreamError(f"Keitaro вернул HTTP {response.status_code}. Проверьте доступ и настройки API.")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError("Keitaro вернул некорректный JSON.") from exc

    def campaigns(self):
        result, seen = [], set()
        for offset in range(0, 100_000, 500):
            batch = self.request("GET", "/campaigns", params={"offset": offset, "limit": 500})
            self._list(batch)
            fresh = [c for c in batch if c["id"] not in seen]
            result.extend(fresh)
            seen.update(c["id"] for c in fresh)
            if len(batch) < 500 or not fresh:
                break
        else:
            raise UpstreamError("Слишком большой список кампаний. Уточните пагинацию на вашем трекере.")
        return result

    @staticmethod
    def _list(value):
        if not isinstance(value, list) or any(not isinstance(i, dict) or "id" not in i for i in value):
            raise UpstreamError("Неожиданный формат списка в Keitaro API.")
        return value

    def campaign(self, campaign_id):
        return self.request("GET", f"/campaigns/{campaign_id}")

    def streams(self, campaign_id):
        return self._list(self.request("GET", f"/campaigns/{campaign_id}/streams"))

    def stream(self, stream_id):
        return self.request("GET", f"/streams/{stream_id}")

    def offers(self):
        return self._list(self.request("GET", "/offers"))

    def offer(self, offer_id):
        return self.request("GET", f"/offers/{offer_id}")

    def lookups(self):
        return {
            "domains": self._list(self.request("GET", "/domains")),
            "groups": self._list(self.request("GET", "/groups", params={"type": "campaigns"})),
            "sources": self._list(self.request("GET", "/traffic_sources")),
        }

    def create_campaign(self, data):
        return self.request("POST", "/campaigns", json=data)

    def create_stream(self, data):
        return self.request("POST", "/streams", json=data)

    def update_campaign(self, campaign_id, data):
        return self.request("PUT", f"/campaigns/{campaign_id}", json=data)

    def update_stream(self, stream_id, data):
        return self.request("PUT", f"/streams/{stream_id}", json=data)

    def archive_campaign(self, campaign_id):
        return self.request("DELETE", f"/campaigns/{campaign_id}")

    def close(self):
        self.client.close()


class DemoBase(DeclarativeBase):
    pass


class Resource(DemoBase):
    __tablename__ = "resources"
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class DemoGateway:
    """Отдельная БД имитирует внешний сервис и переживает перезапуск приложения."""

    def __init__(self, settings):
        if settings.database_url == settings.demo_database_url:
            raise ValueError("Основная БД и демотрекер должны быть разными базами.")
        self.engine = make_engine(settings.demo_database_url)
        DemoBase.metadata.create_all(self.engine)
        self.sessions = make_sessions(self.engine)
        with self.sessions() as db, writing(db):
            if db.get(Resource, ("campaigns", 1)):
                return
            data = {
                "domains": [{"id": 1, "name": "track.example.test", "state": "active"}],
                "groups": [{"id": 1, "name": "Тестовые кампании", "type": "campaigns"}],
                "sources": [{"id": 1, "name": "Facebook", "state": "active"}],
                "offers": [
                    {"id": 101, "name": "Malina · Румыния · основной", "state": "active"},
                    {"id": 102, "name": "Malina · Румыния · альтернативный", "state": "active"},
                    {"id": 103, "name": "Solar · Австралия · заявка", "state": "active"},
                    {"id": 104, "name": "Casa · Мексика · каталог", "state": "active"},
                    {"id": 105, "name": "Nord · Германия · заявка", "state": "active"},
                    {"id": 106, "name": "Vento · Италия · каталог", "state": "active"},
                ],
                "campaigns": [
                    {
                        "id": 1,
                        "name": "Malina / Румыния",
                        "alias": "malina-ro",
                        "type": "position",
                        "state": "active",
                        "domain_id": 1,
                        "group_id": 1,
                        "traffic_source_id": 1,
                    }
                ],
                "streams": [
                    {
                        "id": 11,
                        "campaign_id": 1,
                        "name": "Геофильтр · AU",
                        "position": 1,
                        "type": "regular",
                        "schema": "redirect",
                        "action_type": "http",
                        "action_payload": "https://www.google.com/",
                        "state": "active",
                        "filters": [{"name": "country", "mode": "accept", "payload": ["AU"]}],
                        "offers": [],
                    },
                    {
                        "id": 12,
                        "campaign_id": 1,
                        "name": "Офферы · основной поток",
                        "position": 2,
                        "type": "regular",
                        "schema": "landings",
                        "action_type": "http",
                        "state": "active",
                        "filters": [],
                        "landings": [],
                        "offers": [
                            {"offer_id": oid, "share": share, "state": "active"}
                            for oid, share in [(101, 34), (102, 33), (103, 33)]
                        ],
                    },
                ],
            }
            for kind, entries in data.items():
                for entry in entries:
                    db.add(Resource(kind=kind, id=entry["id"], payload=entry))

    def _all(self, kind):
        with self.sessions() as db:
            return [
                deepcopy(r.payload)
                for r in db.scalars(select(Resource).where(Resource.kind == kind).order_by(Resource.id))
            ]

    def _get(self, kind, rid):
        with self.sessions() as db:
            row = db.get(Resource, (kind, rid))
            if row is None:
                raise AppError("Объект не найден в демотрекере.", 404)
            return deepcopy(row.payload)

    def _write(self, kind, data, rid=None):
        with self.sessions() as db, writing(db):
            if rid is None:
                existing = list(db.scalars(select(Resource.id).where(Resource.kind == kind)))
                rid = max(existing, default=0) + 1
                row = Resource(kind=kind, id=rid, payload={})
                db.add(row)
            else:
                row = db.get(Resource, (kind, rid))
                if row is None:
                    raise AppError("Объект не найден в демотрекере.", 404)
            row.payload = {**row.payload, **deepcopy(data), "id": rid}
            return deepcopy(row.payload)

    def campaigns(self):
        return [c for c in self._all("campaigns") if c.get("state") != "deleted"]

    def campaign(self, campaign_id):
        return self._get("campaigns", campaign_id)

    def streams(self, campaign_id):
        return [s for s in self._all("streams") if s["campaign_id"] == campaign_id and s.get("state") != "deleted"]

    def stream(self, stream_id):
        return self._get("streams", stream_id)

    def offers(self):
        return self._all("offers")

    def offer(self, offer_id):
        return self._get("offers", offer_id)

    def lookups(self):
        return {kind: self._all(kind) for kind in ("domains", "groups", "sources")}

    def create_campaign(self, data):
        return self._write("campaigns", data)

    def create_stream(self, data):
        return self._write("streams", data)

    def update_campaign(self, campaign_id, data):
        return self._write("campaigns", data, campaign_id)

    def update_stream(self, stream_id, data):
        return self._write("streams", data, stream_id)

    def archive_campaign(self, campaign_id):
        return self.update_campaign(campaign_id, {"state": "deleted"})

    def close(self):
        self.engine.dispose()
