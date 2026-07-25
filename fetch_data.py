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
import gzip
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
OWN_STORE_NAME = "Основной склад"
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
    headers = {**headers, "Accept-Encoding": "gzip"}
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers.get("Content-Encoding") == "gzip":
            try: raw = gzip.decompress(raw)
            except OSError: pass
        raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: {raw.decode('utf-8','replace')[:400]}")


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

def _find_wb_rows(node, depth=0):
    """Рекурсивно ищет в ответе WB список строк с товарными полями."""
    if depth > 6:
        return None
    if isinstance(node, list):
        if node and isinstance(node[0], dict) and any(
                k in node[0] for k in ("vendorCode", "supplierArticle", "nmID", "nmId")):
            return node
        for x in node:
            r = _find_wb_rows(x, depth + 1)
            if r:
                return r
    elif isinstance(node, dict):
        for v in node.values():
            r = _find_wb_rows(v, depth + 1)
            if r:
                return r
    return None


def _shape(node, depth=0):
    """Краткое описание структуры ответа — для диагностики в логе."""
    if depth > 3:
        return "…"
    if isinstance(node, dict):
        return "{" + ", ".join(f"{k}: {_shape(v, depth+1)}" for k, v in list(node.items())[:8]) + "}"
    if isinstance(node, list):
        return f"[{len(node)} шт: {_shape(node[0], depth+1) if node else ''}]"
    return type(node).__name__


def fetch_wb_cards() -> dict:
    """Справочник карточек WB: nmID -> (артикул продавца, название).
    Требует у токена категорию «Контент»."""
    url = "https://content-api.wildberries.ru/content/v2/get/cards/list"
    headers = {"Authorization": WB_API_KEY, "Content-Type": "application/json"}
    cards, cursor = {}, {"limit": 100}
    while True:
        d = http_json(url, headers, {"settings": {"cursor": cursor,
                                                  "filter": {"withPhoto": -1}}})
        items = (d.get("cards") or [])
        for c in items:
            nm = c.get("nmID") or c.get("nmId")
            if nm:
                cards[int(nm)] = ((c.get("vendorCode") or "").strip(),
                                  (c.get("title") or c.get("subjectName") or "").strip())
        cur = d.get("cursor") or {}
        if len(items) < 100:
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
    if not cards:
        raise RuntimeError("справочник карточек пуст — добавьте токену категорию «Контент»")
    print(f"WB карточки: {len(cards)}")
    return cards


def fetch_wb_rows() -> list:
    """Остатки WB: stocks-report/wb-warehouses (nmId+склад+количество)
    + справочник карточек для получения артикула продавца."""
    if not WB_API_KEY:
        raise RuntimeError("секрет WB_API_KEY не задан")
    try:
        cards = fetch_wb_cards()
    except Exception as e:
        raise RuntimeError(f"карточки товаров: {e} — токену WB нужны категории "
                           f"«Аналитика» и «Контент»")
    url = "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses"
    headers = {"Authorization": WB_API_KEY, "Content-Type": "application/json"}
    PSEUDO = ("В ПУТИ", "ВСЕГО", "TO THE CLIENT", "FROM THE CLIENT", "ИТОГО")

    def is_pseudo(name: str) -> bool:
        u = (name or "").upper()
        return any(p in u for p in PSEUDO)

    agg, offset = {}, 0
    while True:
        d = http_json(url, headers, {"offset": offset, "limit": 1000})
        rows = _find_wb_rows(d)
        if not rows:
            break
        for r in rows:
            nm = r.get("nmId") or r.get("nmID")
            art, title = cards.get(int(nm), ("", "")) if nm else ("", "")
            if not art:
                art = (r.get("vendorCode") or r.get("supplierArticle") or "").strip()
            if not art:
                continue
            name = f"{title} ({art})" if title else art
            wh = (r.get("warehouseName") or "").strip()
            qty = int(r.get("quantity") or 0)
            if not wh or qty <= 0 or is_pseudo(wh):
                continue
            key = (wh, art)
            if key not in agg:
                agg[key] = {"wh": wh, "art": art, "name": name, "qty": 0}
            agg[key]["qty"] += qty
        if len(rows) < 1000:
            break
        offset += len(rows)
    out = list(agg.values())
    if not out:
        raise RuntimeError("остатки WB пусты после связки с карточками — "
                           "пришлите лог workflow")
    print(f"WB остатки: позиций склад×товар — {len(out)}")
    return out


# ================= МойСклад =================

def fetch_own_rows() -> list:
    """Остатки ТОЛЬКО основного склада из МойСклад + себестоимость оттуда же.
    Деньги в МойСклад приходят в копейках — делим на 100."""
    if not MS_TOKEN:
        raise RuntimeError("секрет MOYSKLAD_TOKEN не задан")
    import urllib.parse
    headers = {"Authorization": f"Bearer {MS_TOKEN}",
               "Accept": "application/json;charset=utf-8",
               "User-Agent": "stockmap/1.0"}

    # 1) находим склад по имени
    flt = urllib.parse.quote(f"name={OWN_STORE_NAME}")
    d = http_json(f"{MS_API}/entity/store?filter={flt}", headers)
    stores = d.get("rows") or []
    if not stores:
        raise RuntimeError(f"склад «{OWN_STORE_NAME}» не найден в МойСклад")
    store_href = stores[0]["meta"]["href"]

    # 2) остатки только по этому складу
    rows, offset, limit = [], 0, 1000
    store_flt = urllib.parse.quote(f"store={store_href}", safe="")
    while True:
        d = http_json(f"{MS_API}/report/stock/all?limit={limit}&offset={offset}"
                      f"&filter={store_flt}", headers)
        part = d.get("rows") or []
        for r in part:
            art = (r.get("article") or r.get("code") or "").strip()
            qty = int(r.get("stock") or 0)
            if not art or qty <= 0:
                continue
            price = r.get("price") or 0  # себестоимость в копейках
            rows.append({
                "art": art,
                "name": (r.get("name") or art).strip(),
                "qty": qty,
                "ms_cost": round(price / 100, 2) if price else None,
            })
        if len(part) < limit:
            break
        offset += limit
    total_qty = sum(r["qty"] for r in rows)
    print(f"МойСклад «{OWN_STORE_NAME}»: позиций — {len(rows)}, штук — {total_qty}")
    return rows


# ================= Себестоимость =================

def _parse_cost_csv(url: str) -> tuple:
    with urllib.request.urlopen(url, timeout=60) as r:
        text = r.read().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    costs, originals = {}, {}
    if not rows:
        return costs, originals
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
            if key not in costs:
                costs[key] = float(raw)
                originals[key] = art
        except ValueError:
            continue
    return costs, originals


def fetch_costs() -> tuple:
    """Возвращает (себесы Ozon-листа, себесы WB-листа) — источники не смешиваются."""
    oz = _parse_cost_csv(COST_CSV_URLS[0])
    wb = _parse_cost_csv(COST_CSV_URLS[1])
    print(f"Себестоимость: Ozon-лист — {len(oz[0])} арт., WB-лист — {len(wb[0])} арт.")
    return oz, wb


# ================= Сборка =================

def main() -> None:
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        print("Не заданы OZON_CLIENT_ID / OZON_API_KEY", file=sys.stderr)
        sys.exit(1)

    source_errors = []
    (costs_oz, orig_oz), (costs_wb, orig_wb) = fetch_costs()

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
    seen_ozon, seen_wb, seen_own = set(), set(), set()

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

    def add_item(w, offer, name, qty, transit, days, over,
                 cost_map=None, fallback_cost=None):
        cost = cost_map.get(norm_art(offer)) if cost_map else None
        if cost is None and fallback_cost:
            cost = fallback_cost
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
        seen_ozon.add(norm_art(offer))
        add_item(wh_entry("ozon", wh_name, loc),
                 offer, (r.get("item_name") or offer).strip(), qty, transit, days, over,
                 cost_map=costs_oz)

    # --- Wildberries (оборачиваемость добавим следующим шагом) ---
    for r in wb_rows:
        loc = locate_wb(r["wh"])
        if loc is None:
            unknown.add(f"WB: {r['wh']}")
        seen_wb.add(norm_art(r["art"]))
        add_item(wh_entry("wb", r["wh"], loc),
                 r["art"], r["name"], r["qty"], 0, None, False,
                 cost_map=costs_wb)

    # --- Собственный склад ---
    if own_rows:
        own = OWN_WAREHOUSE
        w = wh_entry("own", own["name"],
                     (own["lat"], own["lon"], own["cluster"], own["addr"]))
        for r in own_rows:
            seen_own.add(norm_art(r["art"]))
            add_item(w, r["art"], r["name"], r["qty"], 0, None, False,
                     fallback_cost=r.get("ms_cost"))

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
        "cost_sheet_unmatched": sorted(
            orig_oz[k] for k in costs_oz if k not in (catalog | seen_ozon)),
        "cost_sheet_unmatched_wb": sorted(
            orig_wb[k] for k in costs_wb if k not in seen_wb) if wb_rows else [],
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
