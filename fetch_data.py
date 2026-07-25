# -*- coding: utf-8 -*-
"""
Сбор данных для Stockmap: Ozon FBO + Wildberries FBO + собственный склад (МойСклад).

Источники:
1. Ozon Seller API: остатки по складам, поставки в пути, продажи за 30 дней.
2. Wildberries Statistics API: остатки по складам WB.
3. МойСклад JSON API: остатки собственного склада.
4. Google-таблицы с себестоимостью (два опубликованных CSV-листа, объединяются).

Секреты (переменные окружения): OZON_CLIENT_ID, OZON_API_KEY — обязательные;
WB_API_KEY, MOYSKLAD_TOKEN — опциональные (без них источник пропускается
с предупреждением на карте, остальное работает).
"""

import csv
import io
import json
import os
import sys
import datetime as dt
import urllib.request
import urllib.error

from warehouses import locate, locate_wb, OWN_WAREHOUSE

OZON_API = "https://api-seller.ozon.ru"
WB_API = "https://statistics-api.wildberries.ru"
MS_API = "https://api.moysklad.ru/api/remap/1.2"

COST_CSV_URLS = [
    # лист себестоимости (Ozon)
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSuZNqSi644CAyXbUrSWiwxfRQSkhrHl0sAZv4fi0SQbUO24WsBAcaE-ST7Rjz0y47qmlGOlWCgCc_u"
    "/pub?gid=1363140693&single=true&output=csv",
    # лист себестоимости (WB, «Себес для Стокмэп»)
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vTUey4qLzrRfpev9dJ8MvHe6bWE8XZ3p934TeI54I365ZirzmjAyUkdqPvWD1qmS7ePcJH_2QOaL2h_"
    "/pub?gid=47073957&single=true&output=csv",
]

TURNOVER_LIMIT_DAYS = 30
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stocks.json")

OZON_CLIENT_ID = os.environ.get("OZON_CLIENT_ID", "")
OZON_API_KEY = os.environ.get("OZON_API_KEY", "")
WB_API_KEY = os.environ.get("WB_API_KEY", "")
MS_TOKEN = os.environ.get("MOYSKLAD_TOKEN", "")

_LOOKALIKES = str.maketrans("АВЕКМНОРСТУХавекмнорстух", "ABEKMHOPCTYXABEKMHOPCTYX")


def norm_art(art: str) -> str:
    return " ".join((art or "").split()).upper().translate(_LOOKALIKES)


def http_json(url: str, headers: dict, payload: dict | None = None) -> dict | list:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: {body}")


def ozon(path: str, payload: dict) -> dict:
    return http_json(OZON_API + path, {
        "Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json"}, payload)


# ================= Ozon =================

def fetch_ozon_stock_rows() -> list:
    rows, offset, limit = [], 0, 1000
    while True:
        d = ozon("/v2/analytics/stock_on_warehouses",
                 {"limit": limit, "offset": offset, "warehouse_type": "ALL"})
        part = (d.get("result") or {}).get("rows") or []
        rows.extend(part)
        if len(part) < limit:
            break
        offset += limit
    print(f"Ozon остатки: строк — {len(rows)}")
    return rows


def fetch_ozon_sales_30d() -> dict:
    date_to = dt.date.today()
    date_from = date_to - dt.timedelta(days=30)
    sales, offset, limit = {}, 0, 1000
    while True:
        d = ozon("/v1/analytics/data", {
            "date_from": str(date_from), "date_to": str(date_to),
            "metrics": ["ordered_units"], "dimension": ["sku"],
            "filters": [], "limit": limit, "offset": offset})
        part = (d.get("result") or {}).get("data") or []
        for r in part:
            try:
                sku = int(r["dimensions"][0]["id"])
                sales[sku] = sales.get(sku, 0) + float(r["metrics"][0] or 0)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        if len(part) < limit:
            break
        offset += limit
    print(f"Ozon продажи 30 дн: SKU — {len(sales)}")
    return sales


def fetch_ozon_catalog() -> set:
    offers, last_id = set(), ""
    while True:
        d = ozon("/v3/product/list",
                 {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000})
        result = d.get("result") or {}
        items = result.get("items") or []
        for it in items:
            offers.add(norm_art(it.get("offer_id") or ""))
        last_id = result.get("last_id") or ""
        if not last_id or len(items) < 1000:
            break
    print(f"Ozon каталог: товаров — {len(offers)}")
    return offers


# ================= Wildberries =================

def fetch_wb_rows() -> list:
    """Остатки WB: список (склад, артикул, название, количество)."""
    if not WB_API_KEY:
        raise RuntimeError("секрет WB_API_KEY не задан")
    data = http_json(WB_API + "/api/v1/supplier/stocks?dateFrom=2020-01-01T00:00:00",
                     {"Authorization": WB_API_KEY})
    if not isinstance(data, list):
        raise RuntimeError(f"неожиданный ответ WB: {str(data)[:200]}")
    agg = {}
    for r in data:
        wh = (r.get("warehouseName") or "").strip()
        art = (r.get("supplierArticle") or "").strip()
        qty = int(r.get("quantity") or 0)
        if not wh or not art or qty <= 0:
            continue
        subject = (r.get("subject") or "").strip()
        name = f"{subject} ({art})" if subject else art
        key = (wh, art)
        if key not in agg:
            agg[key] = {"wh": wh, "art": art, "name": name, "qty": 0}
        agg[key]["qty"] += qty
    rows = list(agg.values())
    print(f"WB остатки: позиций склад×товар — {len(rows)}")
    return rows


# ================= МойСклад =================

def fetch_own_rows() -> list:
    """Остатки собственного склада из МойСклад: (артикул, название, количество)."""
    if not MS_TOKEN:
        raise RuntimeError("секрет MOYSKLAD_TOKEN не задан")
    rows, offset, limit = [], 0, 1000
    headers = {"Authorization": f"Bearer {MS_TOKEN}",
               "Accept": "application/json;charset=utf-8"}
    while True:
        d = http_json(f"{MS_API}/report/stock/all?limit={limit}&offset={offset}", headers)
        part = d.get("rows") or []
        for r in part:
            art = (r.get("article") or r.get("code") or "").strip()
            qty = int(r.get("stock") or 0)
            if not art or qty <= 0:
                continue
            rows.append({"art": art, "name": (r.get("name") or art).strip(), "qty": qty})
        if len(part) < limit:
            break
        offset += limit
    print(f"МойСклад: позиций — {len(rows)}")
    return rows


# ================= Себестоимость =================

def fetch_costs() -> tuple:
    """Объединяет все листы себестоимости. Возвращает (норм->цена, норм->оригинал)."""
    costs, originals = {}, {}
    for url in COST_CSV_URLS:
        with urllib.request.urlopen(url, timeout=60) as r:
            text = r.read().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            continue
        header = [h.strip().lower() for h in rows[0]]
        try:
            i_art = next(i for i, h in enumerate(header) if "артикул" in h)
            i_cost = next(i for i, h in enumerate(header) if "себес" in h)
        except StopIteration:
            i_art, i_cost = 0, 1
            rows.insert(0, [])
        for row in rows[1:]:
            if len(row) <= max(i_art, i_cost):
                continue
            art = row[i_art].strip()
            raw = row[i_cost].strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
            if not art or not raw:
                continue
            try:
                key = norm_art(art)
                if key not in costs:  # первый лист в приоритете
                    costs[key] = float(raw)
                    originals[key] = art
            except ValueError:
                continue
    print(f"Себестоимость: артикулов (оба листа) — {len(costs)}")
    return costs, originals


# ================= Сборка =================

def main() -> None:
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        print("Не заданы OZON_CLIENT_ID / OZON_API_KEY", file=sys.stderr)
        sys.exit(1)

    source_errors = []
    costs, cost_originals = fetch_costs()

    ozon_rows = fetch_ozon_stock_rows()
    sales = fetch_ozon_sales_30d()
    try:
        catalog = fetch_ozon_catalog()
    except Exception as e:
        print(f"Каталог Ozon недоступен: {e}", file=sys.stderr)
        catalog = set()

    try:
        wb_rows = fetch_wb_rows()
    except Exception as e:
        source_errors.append(f"Wildberries: {e}")
        print(f"WB пропущен: {e}", file=sys.stderr)
        wb_rows = []

    try:
        own_rows = fetch_own_rows()
    except Exception as e:
        source_errors.append(f"МойСклад: {e}")
        print(f"МойСклад пропущен: {e}", file=sys.stderr)
        own_rows = []

    warehouses = {}
    unknown = set()
    no_cost = set()
    seen_offers = set()

    def wh_entry(src, name, loc):
        key = (src, name)
        if key not in warehouses:
            warehouses[key] = {
                "name": name, "src": src,
                "lat": loc[0] if loc else None,
                "lon": loc[1] if loc else None,
                "cluster": loc[2] if loc else "Не распознан",
                "addr": loc[3] if loc else "",
                "stock_value": 0.0, "qty": 0,
                "transit_qty": 0, "transit_value": 0.0,
                "items": [],
            }
        return warehouses[key]

    def add_item(w, offer, name, qty, transit, days, over):
        seen_offers.add(norm_art(offer))
        cost = costs.get(norm_art(offer))
        if cost is None:
            no_cost.add(offer)
        value = round(qty * cost, 2) if cost is not None else 0.0
        t_value = round(transit * cost, 2) if cost is not None else 0.0
        w["qty"] += qty
        w["stock_value"] += value
        w["transit_qty"] += transit
        w["transit_value"] += t_value
        w["items"].append({
            "offer_id": offer, "name": name, "qty": qty, "transit": transit,
            "cost": cost, "value": value, "days": days,
            "over": bool(over and qty > 0), "nocost": cost is None,
        })

    # --- Ozon ---
    for r in ozon_rows:
        wh_name = (r.get("warehouse_name") or "").strip()
        if not wh_name:
            continue
        offer = (r.get("item_code") or "").strip()
        sku = r.get("sku") or 0
        qty = int(r.get("free_to_sell_amount") or 0)
        transit = int(r.get("promised_amount") or 0)
        if qty <= 0 and transit <= 0:
            continue
        loc = locate(wh_name)
        if loc is None:
            unknown.add(f"Ozon: {wh_name}")
        daily = sales.get(int(sku), 0) / 30.0 if sku else 0.0
        if daily > 0:
            days = round(qty / daily, 1)
            over = days > TURNOVER_LIMIT_DAYS
        else:
            days = None
            over = qty > 0
        add_item(wh_entry("ozon", wh_name, loc),
                 offer, (r.get("item_name") or offer).strip(), qty, transit, days, over)

    # --- Wildberries (оборачиваемость добавим следующим шагом) ---
    for r in wb_rows:
        loc = locate_wb(r["wh"])
        if loc is None:
            unknown.add(f"WB: {r['wh']}")
        add_item(wh_entry("wb", r["wh"], loc),
                 r["art"], r["name"], r["qty"], 0, None, False)

    # --- Собственный склад ---
    if own_rows:
        own = OWN_WAREHOUSE
        w = wh_entry("own", own["name"],
                     (own["lat"], own["lon"], own["cluster"], own["addr"]))
        for r in own_rows:
            add_item(w, r["art"], r["name"], r["qty"], 0, None, False)

    for w in warehouses.values():
        w["stock_value"] = round(w["stock_value"], 2)
        w["transit_value"] = round(w["transit_value"], 2)
        w["items"].sort(key=lambda i: i["value"], reverse=True)

    # для сверки листа себестоимости: каталог Ozon + всё, что видели в WB и МойСклад
    known_offers = catalog | seen_offers

    out = {
        "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).isoformat(timespec="minutes"),
        "turnover_limit_days": TURNOVER_LIMIT_DAYS,
        "warehouses": sorted(warehouses.values(), key=lambda w: w["stock_value"], reverse=True),
        "unknown_warehouses": sorted(unknown),
        "offers_without_cost": sorted(no_cost),
        "cost_sheet_unmatched": sorted(
            cost_originals[k] for k in costs if k not in known_offers),
        "source_errors": source_errors,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for src in ("ozon", "wb", "own"):
        vals = [w for w in warehouses.values() if w["src"] == src]
        total = sum(w["stock_value"] for w in vals)
        print(f"{src}: складов {len(vals)}, сток {total:,.0f} ₽")


if __name__ == "__main__":
    main()
