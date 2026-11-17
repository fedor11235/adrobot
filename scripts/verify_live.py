"""Проверка на собственной новой кампании. Запуск: python -m scripts.verify_live --name test_фамилия_…"""

import argparse
import json
from datetime import UTC, datetime

import httpx

from app.config import Settings
from app.gateway import KeitaroGateway


def verify(name, base_url):
    if not name.startswith("test_"):
        raise ValueError("Имя проверочной кампании должно начинаться с test_")
    settings = Settings()
    if settings.app_mode != "live":
        raise ValueError("Для этой проверки в .env должен быть APP_MODE=live")
    auth = (
        (settings.app_username, settings.app_password.get_secret_value())
        if settings.app_password.get_secret_value()
        else None
    )
    gateway = KeitaroGateway(settings)
    campaign_id = None
    checks = []
    with httpx.Client(base_url=base_url, timeout=120, auth=auth) as client:

        def request(method, path, data=None, status=200):
            result = client.request(method, "/api" + path, json=data)
            if result.status_code != status:
                raise RuntimeError(f"{method} {path}: HTTP {result.status_code}; {result.text[:400]}")
            return result.json()

        assert request("GET", "/config")["mode"] == "live"
        offers = request("GET", "/offers")
        ids = [o["id"] for o in offers[:4]]
        if len(ids) < 4:
            raise ValueError("Для сценария нужны четыре доступных оффера")
        a, b, c, d = ids
        try:
            campaign = request("POST", "/campaigns", {"name": name, "geo": "AU", "offer_id": a}, status=201)
            campaign_id = campaign["id"]
            sid = next(s["id"] for s in campaign["streams"] if s["editable"])
            remote = gateway.streams(campaign_id)
            first = min(remote, key=lambda s: s["position"])
            assert first["action_payload"].rstrip("/") == "https://www.google.com"
            assert first["filters"][0]["payload"] == ["AU"]
            assert (
                campaign["metadata"]["domain_id"]
                and campaign["metadata"]["group_id"]
                and campaign["metadata"]["traffic_source_id"]
            )
            checks.append("Создание кампании, настроек и двух упорядоченных потоков")

            def local():
                return next(s for s in request("GET", f"/campaigns/{campaign_id}")["streams"] if s["id"] == sid)

            def change(action, payload=None, method="POST", status=200):
                return request(
                    method, f"/streams/{sid}/{action}", {"revision": local()["revision"], **(payload or {})}, status
                )

            for oid in [b, c]:
                change("offers", {"offer_id": oid})
            change(f"offers/{a}", {"pinned": True, "share": 25}, "PATCH")
            state = change("offers", {"offer_id": d})
            assert sorted(o["share"] for o in state["offers"]) == [25] * 4
            assert len(gateway.stream(sid)["offers"]) == 1
            checks.append("Черновик и пересчёт долей с закреплёнными 25%")
            assert not change("publish")["dirty"]
            assert len(gateway.stream(sid)["offers"]) == 4
            checks.append("Публикация с контрольным чтением")

            change(f"offers/{d}/remove")
            assert not change("cancel")["dirty"]
            assert len(gateway.stream(sid)["offers"]) == 4
            checks.append("Отмена локального удаления")
            change(f"offers/{d}/remove")
            change("publish")
            assert d not in [o["offer_id"] for o in gateway.stream(sid)["offers"]]
            assert next(o for o in local()["offers"] if o["offer_id"] == d)["removed"]
            change(f"offers/{d}/restore")
            change("publish")
            checks.append("Удаление, архив и восстановление оффера")

            gateway.update_stream(
                sid,
                {"offers": [{"offer_id": a, "share": 25}, {"offer_id": c, "share": 38}, {"offer_id": d, "share": 37}]},
            )
            request("POST", f"/campaigns/{campaign_id}/fetch")
            assert next(o for o in local()["offers"] if o["offer_id"] == b)["removed"]
            change(f"offers/{b}/restore")
            change("publish")
            checks.append("Внешнее удаление в Keitaro и последующее восстановление")

            change(f"offers/{d}/remove")
            gateway.update_stream(
                sid,
                {"offers": [{"offer_id": oid, "share": share} for oid, share in [(a, 25), (b, 35), (c, 15), (d, 25)]]},
            )
            change("publish", status=409)
            assert local()["dirty"]
            change("cancel")
            request("POST", f"/campaigns/{campaign_id}/fetch")
            change("balance")
            change("publish")
            assert len(gateway.stream(sid)["offers"]) == 4
            checks.append("Защита от конфликта и повторная синхронизация")
            return {
                "checked_at": datetime.now(UTC).isoformat(),
                "campaign_id": campaign_id,
                "stream_id": sid,
                "campaign_name": name,
                "checks": checks,
                "final_state": "disabled",
            }
        finally:
            if campaign_id:
                # Структуру оставляем проверяющему, саму кампанию останавливаем.
                gateway.update_campaign(campaign_id, {"state": "disabled"})
                print(f"Проверочная кампания #{campaign_id} остановлена.")
            gateway.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--app-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    print(json.dumps(verify(args.name, args.app_url), ensure_ascii=False, indent=2))
