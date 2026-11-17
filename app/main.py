import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app import services
from app.config import Settings
from app.db import make_engine, make_sessions, writing
from app.errors import AppError
from app.gateway import DemoGateway, KeitaroGateway
from app.schemas import AddOffer, CreateCampaign, DemoOffers, PinOffer, Revision
from app.weights import redistribute

STATIC = Path(__file__).parent / "static"
basic = HTTPBasic(auto_error=False)


def create_app(settings=None, gateway=None):
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)

    @asynccontextmanager
    async def lifespan(app):
        if not inspect(engine).has_table("streams"):
            raise RuntimeError("База не подготовлена. Выполните: uv run alembic upgrade head")
        app.state.gateway = gateway or (
            DemoGateway(settings) if settings.app_mode == "demo" else KeitaroGateway(settings)
        )
        yield
        if gateway is None:
            app.state.gateway.close()
        engine.dispose()

    def authenticate(credentials: HTTPBasicCredentials | None = Depends(basic)):
        password = settings.app_password.get_secret_value()
        if password and (
            credentials is None
            or not secrets.compare_digest(credentials.username.encode(), settings.app_username.encode())
            or not secrets.compare_digest(credentials.password.encode(), password.encode())
        ):
            from fastapi import HTTPException

            raise HTTPException(401, "Требуется вход", headers={"WWW-Authenticate": "Basic"})

    app = FastAPI(title="Adrobot", version="1.0.0", lifespan=lifespan, dependencies=[Depends(authenticate)])
    app.state.settings = settings
    app.state.sessions = sessions

    def database():
        with sessions() as db:
            yield db

    def remote(request: Request):
        return request.app.state.gateway

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        details = [{"field": ".".join(map(str, e["loc"][1:])), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": "Проверьте поля формы.", "errors": details}, status_code=422)

    @app.exception_handler(OperationalError)
    async def database_error(_, exc):
        return JSONResponse({"detail": "База данных временно недоступна. Повторите запрос."}, status_code=503)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health(db=Depends(database)):
        db.execute(text("SELECT 1"))
        return {"status": "ok", "mode": settings.app_mode}

    @app.get("/api/config")
    def config():
        return {
            "mode": settings.app_mode,
            "defaults": {
                "domain_id": settings.default_domain_id,
                "group_id": settings.default_group_id,
                "source_id": settings.default_source_id,
            },
        }

    @app.get("/api/lookups")
    def lookups(g=Depends(remote)):
        return {
            kind: [
                {"id": x["id"], "name": x.get("name", str(x["id"]))}
                for x in entries
                if x.get("state") not in {"deleted", "disabled"}
            ]
            for kind, entries in g.lookups().items()
        }

    @app.get("/api/offers")
    def offers(q: str = "", g=Depends(remote)):
        needle = q.casefold().strip()
        entries = [
            o
            for o in g.offers()
            if o.get("state") not in {"deleted", "disabled"}
            and (needle in o.get("name", "").casefold() or needle in str(o["id"]))
        ]
        return [{"id": o["id"], "name": o["name"]} for o in entries[:30]]

    @app.get("/api/campaigns")
    def campaigns(g=Depends(remote)):
        return [
            {"id": c["id"], "name": c["name"], "alias": c.get("alias", "")}
            for c in g.campaigns()
            if c.get("state") != "deleted"
        ]

    @app.post("/api/campaigns", status_code=201)
    def create(data: CreateCampaign, db=Depends(database), g=Depends(remote)):
        with writing(db):
            campaign = services.create_campaign(db, g, settings, data)
            result = services.campaign_view(campaign)
        return result

    @app.get("/api/campaigns/{campaign_id}")
    def campaign(campaign_id: int, db=Depends(database)):
        return services.campaign_view(services.get_campaign(db, campaign_id))

    @app.post("/api/campaigns/{campaign_id}/fetch")
    def fetch(campaign_id: int, db=Depends(database), g=Depends(remote)):
        with writing(db):
            result = services.campaign_view(services.fetch_campaign(db, g, campaign_id))
        return result

    def mutate(db, stream_id, data, action):
        with writing(db):
            stream = services.locked_stream(db, stream_id, data.revision)
            action(stream)
            db.flush()
            result = services.stream_view(stream)
        return result

    @app.post("/api/streams/{stream_id}/offers")
    def add(stream_id: int, data: AddOffer, db=Depends(database), g=Depends(remote)):
        return mutate(db, stream_id, data, lambda s: services.add_offer(s, g, data.offer_id))

    @app.post("/api/streams/{stream_id}/offers/{offer_id}/remove")
    def remove(stream_id: int, offer_id: int, data: Revision, db=Depends(database)):
        return mutate(db, stream_id, data, lambda s: services.change_offer(s, offer_id, "remove"))

    @app.post("/api/streams/{stream_id}/offers/{offer_id}/restore")
    def restore(stream_id: int, offer_id: int, data: Revision, db=Depends(database), g=Depends(remote)):
        def action(stream):
            services.find_offer(stream, offer_id)
            services.add_offer(stream, g, offer_id)

        return mutate(db, stream_id, data, action)

    @app.patch("/api/streams/{stream_id}/offers/{offer_id}")
    def pin(stream_id: int, offer_id: int, data: PinOffer, db=Depends(database)):
        return mutate(db, stream_id, data, lambda s: services.change_offer(s, offer_id, "pin", data.pinned, data.share))

    @app.post("/api/streams/{stream_id}/balance")
    def balance(stream_id: int, data: Revision, db=Depends(database)):
        def action(stream):
            services.require_editable(stream)
            redistribute(stream.offers)
            stream.revision += 1

        return mutate(db, stream_id, data, action)

    @app.post("/api/streams/{stream_id}/cancel")
    def cancel(stream_id: int, data: Revision, db=Depends(database)):
        return mutate(db, stream_id, data, services.cancel)

    @app.post("/api/streams/{stream_id}/publish")
    def publish(stream_id: int, data: Revision, db=Depends(database), g=Depends(remote)):
        return mutate(db, stream_id, data, lambda s: services.publish(s, g))

    if settings.app_mode == "demo":

        @app.put("/api/demo/streams/{stream_id}/offers", tags=["Демотрекер"])
        def demo_edit(stream_id: int, data: DemoOffers, g=Depends(remote)):
            """Имитирует внешнее изменение Keitaro, не затрагивая локальный черновик."""
            seen = set()
            for item in data.offers:
                oid, share = item.get("offer_id"), item.get("share")
                if (
                    type(oid) is not int
                    or oid <= 0
                    or oid in seen
                    or type(share) is not int
                    or not 0 <= share <= 100
                    or item.get("state", "active") not in {"active", "disabled"}
                ):
                    raise AppError("Ожидаются уникальные offer_id, доли 0–100 и допустимый state.")
                g.offer(oid)
                seen.add(oid)
            if data.offers and sum(o["share"] for o in data.offers if o.get("state", "active") == "active") != 100:
                raise AppError("Сумма активных долей демотрекера должна быть 100%.")
            g.update_stream(stream_id, {"offers": data.offers})
            return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
