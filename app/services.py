from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, UpstreamError
from app.models import Campaign, Stream, StreamOffer, utcnow
from app.weights import redistribute


def current(stream):
    return sorted(
        [
            {
                "offer_id": o.offer_id,
                "name": o.name,
                "share": o.share,
                "pinned": o.pinned,
                "removed": o.removed,
                "remote_state": o.remote_state,
            }
            for o in stream.offers
        ],
        key=lambda o: o["offer_id"],
    )


def dirty(stream):
    return current(stream) != stream.baseline


def signature(remote):
    """Сравниваем только изменяемую область и тип потока; чужие фильтры не трогаем."""
    return {
        "schema": remote.get("schema"),
        "state": remote.get("state", "active"),
        "offers": sorted(
            [
                {"offer_id": int(o["offer_id"]), "share": int(o.get("share", 0)), "state": o.get("state", "active")}
                for o in remote.get("offers", [])
            ],
            key=lambda o: o["offer_id"],
        ),
    }


def stream_view(stream):
    offers = current(stream)
    active = [o for o in offers if not o["removed"] and o["remote_state"] == "active"]
    return {
        "id": stream.id,
        "name": stream.name,
        "position": stream.position,
        "revision": stream.revision,
        "dirty": dirty(stream),
        "editable": stream.editable,
        "remote_missing": stream.remote_missing,
        "synced_at": stream.synced_at.isoformat(),
        "offers": offers,
        "total": sum(o["share"] for o in active),
        "active_count": len(active),
    }


def campaign_view(campaign):
    return {
        "id": campaign.id,
        "name": campaign.name,
        "alias": campaign.alias,
        "metadata": campaign.metadata_json,
        "synced_at": campaign.synced_at.isoformat(),
        "streams": [stream_view(s) for s in sorted(campaign.streams, key=lambda s: (s.position, s.id))],
    }


def get_campaign(db, campaign_id):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise AppError("Сначала загрузите потоки кампании из Keitaro.", 404)
    return campaign


def locked_stream(db: Session, stream_id, revision):
    stream = db.scalar(select(Stream).where(Stream.id == stream_id).with_for_update())
    if not stream:
        raise AppError("Поток не найден. Загрузите кампанию заново.", 404)
    if stream.revision != revision:
        raise AppError("Поток изменился в другом окне. Обновите страницу и повторите действие.", 409)
    return stream


def require_editable(stream):
    if stream.remote_missing:
        raise AppError("Поток удалён в Keitaro. Редактирование недоступно.", 409)
    if not stream.editable:
        raise AppError("Этот поток не содержит офферов и отображается без редактирования.")


def find_offer(stream, offer_id):
    offer = next((o for o in stream.offers if o.offer_id == offer_id), None)
    if not offer:
        raise AppError("Оффер не найден в этом потоке.", 404)
    return offer


def merge_stream(stream, remote, names):
    known = {o.offer_id: o for o in stream.offers}
    present = set()
    for item in remote.get("offers", []):
        oid = int(item["offer_id"])
        if oid in present:
            raise UpstreamError("Keitaro вернул повторяющиеся ID офферов в потоке.")
        present.add(oid)
        share = int(item.get("share", 0))
        if not 0 <= share <= 100:
            raise UpstreamError("Keitaro вернул долю оффера вне диапазона 0–100.")
        offer = known.get(oid)
        if offer is None:
            offer = StreamOffer(offer_id=oid, name="", pinned=False)
            stream.offers.append(offer)
        offer.name = names.get(oid, offer.name or f"Оффер #{oid}")
        offer.share, offer.removed = share, False
        offer.remote_state = item.get("state", "active")
    for oid, offer in known.items():
        if oid not in present:
            offer.removed, offer.share, offer.pinned = True, 0, False
    stream.editable = stream.editable or bool(present)
    stream.remote_snapshot = deepcopy(remote)
    stream.remote_missing = False
    stream.baseline = current(stream)


def fetch_campaign(db, gateway, campaign_id):
    campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    # Блокируем те же строки, что и локальные правки/публикация.
    list(db.scalars(select(Stream).where(Stream.campaign_id == campaign_id).with_for_update()))
    remote_campaign = gateway.campaign(campaign_id)
    remote_streams = gateway.streams(campaign_id)
    names = {int(o["id"]): o["name"] for o in gateway.offers()}
    if campaign is None:
        campaign = Campaign(id=campaign_id, name=remote_campaign["name"])
        db.add(campaign)
        db.flush()
    known = {s.id: s for s in campaign.streams}
    present = {int(r["id"]) for r in remote_streams}
    # Проверяем все конфликты до слияния. Ошибка откатывает всю синхронизацию.
    for stream in known.values():
        if dirty(stream):
            remote = next((r for r in remote_streams if int(r["id"]) == stream.id), None)
            if remote is None or signature(remote) != signature(stream.remote_snapshot):
                raise AppError(
                    f"Поток «{stream.name}» изменён в Keitaro и имеет локальный черновик. Отмените черновик перед загрузкой.",
                    409,
                )
    for remote in remote_streams:
        sid = int(remote["id"])
        stream = known.get(sid)
        if stream is None:
            stream = Stream(id=sid, name=remote["name"], editable=False, revision=0, baseline=[])
            campaign.streams.append(stream)
        stream.name = remote["name"]
        stream.position = int(remote.get("position", 0))
        if not dirty(stream):
            merge_stream(stream, remote, names)
        else:
            stream.remote_snapshot = deepcopy(remote)
        stream.synced_at, stream.revision = utcnow(), stream.revision + 1
    for sid, stream in known.items():
        if sid not in present:
            stream.remote_missing = True
            stream.revision += 1
    campaign.name = remote_campaign["name"]
    campaign.alias = remote_campaign.get("alias", "")
    campaign.metadata_json = {
        k: remote_campaign.get(k) for k in ("domain_id", "group_id", "traffic_source_id", "state")
    }
    campaign.synced_at = utcnow()
    db.flush()
    return campaign


def add_offer(stream, gateway, offer_id):
    require_editable(stream)
    existing = next((o for o in stream.offers if o.offer_id == offer_id), None)
    if existing and not existing.removed:
        raise AppError("Этот оффер уже есть в потоке.", 409)
    data = gateway.offer(offer_id)
    if data.get("state") in {"deleted", "disabled"}:
        raise AppError("Оффер неактивен в Keitaro.")
    if existing:
        existing.removed, existing.pinned, existing.remote_state = False, False, "active"
        existing.name = data["name"]
    else:
        stream.offers.append(
            StreamOffer(
                offer_id=offer_id, name=data["name"], share=0, pinned=False, removed=False, remote_state="active"
            )
        )
    redistribute(stream.offers)
    stream.revision += 1


def change_offer(stream, offer_id, operation, pinned=None, share=None):
    require_editable(stream)
    offer = find_offer(stream, offer_id)
    if operation == "remove":
        if offer.removed:
            raise AppError("Оффер уже удалён.", 409)
        offer.removed, offer.pinned, offer.share = True, False, 0
    elif operation == "restore":
        if not offer.removed:
            raise AppError("Оффер уже активен.", 409)
        offer.removed, offer.pinned, offer.remote_state = False, False, "active"
    elif operation == "pin":
        if offer.removed or offer.remote_state != "active":
            raise AppError("Закреплять можно только активные офферы.")
        offer.pinned = pinned
        if share is not None:
            if not pinned:
                raise AppError("Для ручного изменения доли закрепите оффер.")
            offer.share = share
    redistribute(stream.offers)
    stream.revision += 1


def cancel(stream):
    # Разрешено даже для исчезнувшего внешнего потока — иначе конфликт нельзя разрешить.
    baseline = {o["offer_id"]: o for o in stream.baseline}
    for offer in list(stream.offers):
        if offer.offer_id not in baseline:
            stream.offers.remove(offer)
        else:
            for key, value in baseline[offer.offer_id].items():
                setattr(offer, key, value)
    stream.revision += 1


def publish(stream, gateway):
    require_editable(stream)
    if not dirty(stream):
        return
    active = [o for o in stream.offers if not o.removed and o.remote_state == "active"]
    if not active:
        raise AppError("Нельзя опубликовать поток без активных офферов. Добавьте или восстановите оффер.")
    if sum(o.share for o in active) != 100:
        raise AppError("Перед публикацией сумма активных долей должна быть равна 100%.")
    offers = [
        {"offer_id": o.offer_id, "share": o.share, "state": o.remote_state}
        for o in sorted(stream.offers, key=lambda o: o.offer_id)
        if not o.removed
    ]
    desired = {**stream.remote_snapshot, "offers": offers}
    remote = gateway.stream(stream.id)
    if signature(remote) != signature(desired):
        if signature(remote) != signature(stream.remote_snapshot):
            raise AppError(
                "Поток изменился в Keitaro. Черновик сохранён. Отмените его и загрузите актуальные потоки.", 409
            )
        # Только offers: не затираем фильтры, лендинги, триггеры и настройки потока.
        gateway.update_stream(stream.id, {"offers": offers})
        remote = gateway.stream(stream.id)
        if signature(remote) != signature(desired):
            raise UpstreamError("Keitaro не подтвердил ожидаемые доли. Черновик сохранён; проверьте поток в трекере.")
    # Если предыдущий PUT прошёл, но его ответ потерялся, повторная публикация безопасна.
    stream.remote_snapshot = deepcopy(remote)
    stream.baseline = current(stream)
    stream.synced_at, stream.revision = utcnow(), stream.revision + 1


def create_campaign(db, gateway, settings, data):
    lookups = gateway.lookups()
    selected = {}
    for field, kind, default in [
        ("domain_id", "domains", settings.default_domain_id),
        ("group_id", "groups", settings.default_group_id),
        ("source_id", "sources", settings.default_source_id),
    ]:
        available = [o for o in lookups[kind] if o.get("state") not in {"deleted", "disabled"}]
        value = getattr(data, field) or default or (available[0]["id"] if available else None)
        if value is None or value not in [o["id"] for o in available]:
            raise AppError(f"Выберите доступное значение для {field}.")
        selected[field] = value
    offer = gateway.offer(data.offer_id)
    if offer.get("state") in {"deleted", "disabled"}:
        raise AppError("Выбранный оффер неактивен в Keitaro.")
    campaign = gateway.create_campaign(
        {
            "name": data.name,
            "alias": f"adrobot-{uuid4().hex[:12]}",
            "type": "position",
            "state": "disabled",
            "domain_id": selected["domain_id"],
            "group_id": str(selected["group_id"]),
            "traffic_source_id": selected["source_id"],
        }
    )
    cid = int(campaign["id"])
    try:
        gateway.create_stream(
            {
                "campaign_id": cid,
                "name": f"Геофильтр · {data.geo}",
                "position": 1,
                "type": "regular",
                "schema": "redirect",
                "action_type": "http",
                "action_payload": "https://www.google.com/",
                "state": "active",
                "collect_clicks": True,
                "filter_or": False,
                "filters": [{"name": "country", "mode": "accept", "payload": data.geo.split(",")}],
            }
        )
        gateway.create_stream(
            {
                "campaign_id": cid,
                "name": "Офферы · основной поток",
                "position": 2,
                "type": "regular",
                "schema": "landings",
                "action_type": "http",
                "state": "active",
                "collect_clicks": True,
                "filter_or": False,
                "filters": [],
                "landings": [],
                "offers": [{"offer_id": data.offer_id, "share": 100, "state": "active"}],
            }
        )
        gateway.update_campaign(cid, {"state": "active"})
    except Exception as exc:
        try:
            gateway.archive_campaign(cid)
        except Exception:
            raise UpstreamError(
                f"Не удалось завершить создание кампании #{cid} и убрать её в архив. Проверьте её вручную в Keitaro."
            ) from exc
        raise UpstreamError(f"Создание кампании #{cid} прервано; кампания перемещена в архив.") from exc
    return fetch_campaign(db, gateway, cid)
