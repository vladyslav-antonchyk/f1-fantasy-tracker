"""Витягує пілотів і команди з довільних JSON-відповідей сайту F1 Fantasy.

Ми не знаємо точної структури API (вона змінюється між сезонами), тому
шукаємо в JSON списки об'єктів, у яких є поле з іменем і поле з ціною.
Назви полів підбираються зі списків кандидатів без урахування регістру.
"""
from __future__ import annotations

import re
from typing import Any

NAME_KEYS = ["fullname", "displayname", "playername", "name", "playerfullname",
             "teamname", "constructorname", "shortname"]
PRICE_KEYS = ["value", "price", "currentprice", "playervalue", "cost", "currentvalue",
              "salary"]
OLD_PRICE_KEYS = ["oldplayervalue", "oldvalue", "previousvalue", "previousprice",
                  "oldprice", "startprice"]
POINTS_KEYS = ["overallpoints", "overallppints", "totalpoints", "seasontotalpoints",
               "points", "fantasypoints", "seasonpoints", "totalpts", "overallpts"]
LAST_POINTS_KEYS = ["gamedaypoints", "lastpoints", "racepoints", "matchdaypoints"]
ID_KEYS = ["playerid", "id", "driverid", "constructorid", "teamid", "abbreviation"]
TEAM_KEYS = ["teamname", "team", "constructor", "constructorname"]
TYPE_KEYS = ["positionname", "position", "skill", "type", "playertype", "category"]
PICKED_KEYS = ["selectedpercentage", "percentagepicked", "selectedper", "picked",
               "ownership", "selectedby"]


def _lower_map(d: dict) -> dict[str, str]:
    return {k.lower().replace("_", ""): k for k in d.keys()}


def _pick(d: dict, lm: dict[str, str], candidates: list[str]):
    for c in candidates:
        if c in lm and d[lm[c]] not in (None, ""):
            return lm[c], d[lm[c]]
    return None, None


def parse_price(v: Any) -> float | None:
    """'13.8M' -> 13.8, 13.8 -> 13.8, 13800000 -> 13.8, '$13.8m' -> 13.8."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
        if not m:
            return None
        x = float(m.group().replace(",", "."))
    if x >= 100_000:          # повна сума в доларах
        x = x / 1_000_000
    elif x >= 100:            # деякі ігри зберігають у десятих частках (138 = 13.8M)
        x = x / 10
    if not 1 <= x <= 60:
        return None
    return round(x, 2)


def parse_num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group().replace(",", ".")) if m else None


def _guess_type(raw_type: Any, name: str, team: str | None) -> str | None:
    if raw_type is not None:
        t = str(raw_type).lower()
        if "driver" in t or t in ("1", "drv", "d"):
            return "driver"
        if "constructor" in t or "team" in t or t in ("2", "con", "c"):
            return "constructor"
    if team and name and team.strip().lower() == name.strip().lower():
        return "constructor"
    return None


def asset_from_obj(d: dict) -> tuple[dict | None, dict]:
    lm = _lower_map(d)
    nk, name = _pick(d, lm, NAME_KEYS)
    pk, price_raw = _pick(d, lm, PRICE_KEYS)
    if not isinstance(name, str) or price_raw is None:
        return None, {}
    price = parse_price(price_raw)
    if price is None:
        return None, {}
    mapping = {"name": nk, "price": pk}
    asset: dict[str, Any] = {"name": name.strip(), "price": price}

    k, v = _pick(d, lm, ID_KEYS)
    asset["id"] = str(v) if v is not None else None
    mapping["id"] = k
    k, v = _pick(d, lm, TEAM_KEYS)
    team = v if isinstance(v, str) else None
    asset["team"] = team
    mapping["team"] = k
    k, v = _pick(d, lm, TYPE_KEYS)
    asset["type"] = _guess_type(v, asset["name"], team)
    mapping["type"] = k
    k, v = _pick(d, lm, POINTS_KEYS)
    if k is None:   # запасний варіант: будь-яке поле на кшталт "...overall/total...p(o)ints"
        for lk, orig in lm.items():
            if re.search(r"(overall|total|season).*p\w*ts?$", lk) and parse_num(d[orig]) is not None:
                k, v = orig, d[orig]
                break
    asset["season_points"] = parse_num(v)
    mapping["season_points"] = k
    k, v = _pick(d, lm, LAST_POINTS_KEYS)
    asset["last_points"] = parse_num(v)
    mapping["last_points"] = k
    k, v = _pick(d, lm, OLD_PRICE_KEYS)
    asset["old_price"] = parse_price(v)
    mapping["old_price"] = k
    k, v = _pick(d, lm, PICKED_KEYS)
    asset["picked_pct"] = parse_num(v)
    mapping["picked_pct"] = k
    return asset, mapping


def _iter_lists(node: Any, path: str = "$"):
    if isinstance(node, list):
        yield path, node
        for i, x in enumerate(node[:200]):
            yield from _iter_lists(x, f"{path}[{i}]")
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_lists(v, f"{path}.{k}")


def find_assets(payload: Any, hint_type: str | None = None) -> list[dict]:
    """Повертає кандидатів: [{path, mapping, assets}] — списки з >=5 гравцями."""
    found = []
    for path, lst in _iter_lists(payload):
        objs = [x for x in lst if isinstance(x, dict)]
        if len(objs) < 5:
            continue
        assets, mapping = [], None
        for o in objs:
            a, m = asset_from_obj(o)
            if a:
                if a["type"] is None and hint_type:
                    a["type"] = hint_type
                assets.append(a)
                mapping = mapping or m
        if len(assets) >= 5 and len(assets) >= 0.6 * len(objs):
            found.append({"path": path, "mapping": mapping, "assets": assets})
    return found


def merge_assets(candidates: list[dict]) -> list[dict]:
    """Об'єднує знайдене з різних відповідей; ключ — (тип, ім'я)."""
    merged: dict[tuple, dict] = {}
    for c in candidates:
        for a in c["assets"]:
            key = (a.get("type"), a["name"].lower())
            cur = merged.get(key)
            if cur is None:
                merged[key] = dict(a)
            else:
                for k, v in a.items():
                    if cur.get(k) is None and v is not None:
                        cur[k] = v
    out = list(merged.values())
    for a in out:
        if not a.get("id"):
            a["id"] = re.sub(r"[^a-z0-9]+", "-", a["name"].lower()).strip("-")
        a["key"] = f"{a.get('type') or 'unknown'}:{a['id']}"
    out.sort(key=lambda a: (a.get("type") or "", -a["price"]))
    return out
