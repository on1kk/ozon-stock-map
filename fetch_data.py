# -*- coding: utf-8 -*-
"""
Сбор данных для карты остатков FBO Ozon.

Источники:
1. API Ozon Seller:
   - /v2/analytics/stock_on_warehouses — остатки и "в пути" по каждому складу;
   - /v1/analytics/data — заказанные штуки за последние 30 дней по каждому SKU
     (для расчёта оборачиваемости).
2. Google-таблица с себестоимостью (опубликованный CSV: Артикул, Себестоимость).

Результат: data/stocks.json, который читает index.html.

Запуск: переменные окружения OZON_CLIENT_ID и OZON_API_KEY обязательны.
"""

import csv
import io
import json
import os
import sys
import datetime as dt
import urllib.request
import urllib.error

from warehouses import locate

API = "https://api-seller.ozon.ru"
COST_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSuZNqSi644CAyXbUrSWiwxfRQSkhrHl0sAZv4fi0SQbUO24WsBAcaE-ST7Rjz0y47qmlGOlWCgCc_u"
    "/pub?gid=1363140693&single=true&output=csv"
)
TURNOVER_LIMIT_DAYS = 30  # порог затоваренности
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stocks.json")

CLIENT_ID = os.environ.get("OZON_CLIENT_ID", "")
API_KEY = os.environ.get("OZON_API_KEY", "")


def ozon(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Client-Id": CLIENT_ID,
            "Api-Key": API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"Ошибка Ozon API {path}: HTTP {e.code}\n{body}", file=sys.stderr)
        raise


def fetch_stock_rows() -> list:
    """Все строки остатков: товар × склад."""
    rows, offset, limit = [], 0, 1000
    while True:
        d = ozon("/v2/analytics/stock_on_warehouses",
                 {"limit": limit, "offset": offset, "warehouse_type": "ALL"})
        part = (d.get("result") or {}).get("rows") or []
        rows.extend(part)
        if len(part) < limit:
            break
        offset += limit
    print(f"Остатки: получено строк — {len(rows)}")
    return rows


def fetch_sales_30d() -> dict:
    """SKU -> заказано штук за последние 30 дней."""
    date_to = dt.date.today()
    date_from = date_to - dt.timedelta(days=30)
    sales, offset, limit = {}, 0, 1000
    while True:
        d = ozon("/v1/analytics/data", {
            "date_from": str(date_from),
            "date_to": str(date_to),
            "metrics": ["ordered_units"],
            "dimension": ["sku"],
            "filters": [],
            "limit": limit,
            "offset": offset,
        })
        part = (d.get("result") or {}).get("data") or []
        for r in part:
            try:
                sku = int(r["dimensions"][0]["id"])
                units = float(r["metrics"][0] or 0)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            sales[sku] = sales.get(sku, 0) + units
        if len(part) < limit:
            break
        offset += limit
    print(f"Продажи за 30 дней: SKU с продажами — {len(sales)}")
    return sales


def fetch_costs() -> dict:
    """Артикул -> себестоимость (из опубликованного CSV Google-таблицы)."""
    with urllib.request.urlopen(COST_CSV_URL, timeout=60) as r:
        text = r.read().decode("utf-8-sig")
    costs = {}
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return costs
    header = [h.strip().lower() for h in rows[0]]
    try:
        i_art = next(i for i, h in enumerate(header) if "артикул" in h)
        i_cost = next(i for i, h in enumerate(header) if "себестоим" in h)
    except StopIteration:
        # если шапки нет — считаем, что колонки A и B
        i_art, i_cost = 0, 1
        rows.insert(0, [])  # чтобы не потерять первую строку
    for row in rows[1:]:
        if len(row) <= max(i_art, i_cost):
            continue
        art = row[i_art].strip()
        raw = row[i_cost].strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
        if not art or not raw:
            continue
        try:
            costs[art] = float(raw)
        except ValueError:
            continue
    print(f"Себестоимость: загружено артикулов — {len(costs)}")
    return costs


def main() -> None:
    if not CLIENT_ID or not API_KEY:
        print("Не заданы OZON_CLIENT_ID / OZON_API_KEY", file=sys.stderr)
        sys.exit(1)

    stock_rows = fetch_stock_rows()
    sales = fetch_sales_30d()
    costs = fetch_costs()

    warehouses = {}   # name -> данные склада
    unknown = set()   # нераспознанные склады
    no_cost = set()   # артикулы без себестоимости

    for r in stock_rows:
        wh_name = (r.get("warehouse_name") or "").strip()
        if not wh_name:
            continue
        offer = (r.get("item_code") or "").strip()
        sku = r.get("sku") or 0
        item_name = (r.get("item_name") or offer).strip()
        qty = int(r.get("free_to_sell_amount") or 0)
        transit = int(r.get("promised_amount") or 0)
        if qty <= 0 and transit <= 0:
            continue

        loc = locate(wh_name)
        if loc is None:
            unknown.add(wh_name)

        cost = costs.get(offer)
        if cost is None:
            no_cost.add(offer)

        daily = sales.get(int(sku), 0) / 30.0 if sku else 0.0
        if daily > 0:
            days = round(qty / daily, 1)
            over = days > TURNOVER_LIMIT_DAYS
        else:
            days = None                # продаж за 30 дней не было
            over = qty > 0             # лежит и не продаётся — затоварен

        w = warehouses.setdefault(wh_name, {
            "name": wh_name,
            "lat": loc[0] if loc else None,
            "lon": loc[1] if loc else None,
            "cluster": loc[2] if loc else "Не распознан",
            "stock_value": 0.0, "qty": 0,
            "transit_qty": 0, "transit_value": 0.0,
            "items": [],
        })
        value = round(qty * cost, 2) if cost is not None else 0.0
        t_value = round(transit * cost, 2) if cost is not None else 0.0
        w["qty"] += qty
        w["stock_value"] += value
        w["transit_qty"] += transit
        w["transit_value"] += t_value
        w["items"].append({
            "offer_id": offer,
            "name": item_name,
            "qty": qty,
            "transit": transit,
            "cost": cost,
            "value": value,
            "days": days,
            "over": bool(over and qty > 0),
            "nocost": cost is None,
        })

    for w in warehouses.values():
        w["stock_value"] = round(w["stock_value"], 2)
        w["transit_value"] = round(w["transit_value"], 2)
        w["items"].sort(key=lambda i: i["value"], reverse=True)

    out = {
        "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).isoformat(timespec="minutes"),
        "turnover_limit_days": TURNOVER_LIMIT_DAYS,
        "warehouses": sorted(warehouses.values(), key=lambda w: w["stock_value"], reverse=True),
        "unknown_warehouses": sorted(unknown),
        "offers_without_cost": sorted(no_cost),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    total = sum(w["stock_value"] for w in warehouses.values())
    print(f"Готово: складов — {len(warehouses)}, сток — {total:,.0f} ₽, "
          f"нераспознанных складов — {len(unknown)}, артикулов без себеса — {len(no_cost)}")


if __name__ == "__main__":
    main()
