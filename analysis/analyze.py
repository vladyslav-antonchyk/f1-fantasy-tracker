"""Аналіз знімків: зміни цін, очки за етап, пороги подорожчання/подешевшання.

Логіка:
- Знімки йдуть у хронологічному порядку. Новий "етап" починається, коли у
  більшості гравців змінились сумарні очки сезону.
- Очки за етап = різниця сумарних очок.
- Зміна ціни за етап = ціна перед наступним етапом − ціна перед цим етапом.
- Метрика = середні очки на мільйон (очки / ціна до зміни) за останні N етапів.
- Для кожної цінової категорії шукаємо межі метрики між групами з різною
  зміною ціни. Якщо даних мало — використовуємо припущення з config.json.

Результат: site/data.json (його читає сайт) і друк звіту в консоль.
Запуск: python analysis/analyze.py
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SNAP_DIR = ROOT / "data" / "snapshots"
OUT = ROOT / "site" / "data.json"


def load_snapshots():
    snaps = []
    for f in sorted(SNAP_DIR.glob("*.json")):
        s = json.loads(f.read_text(encoding="utf-8"))
        s["by_key"] = {a["key"]: a for a in s["assets"]}
        snaps.append(s)
    return snaps


def build_rounds(snaps):
    """Повертає список етапів: {start_snap, end_snap} та інфо по гравцях."""
    if not snaps:
        return [], {}
    # розбиваємо знімки на "епохи" — між змінами очок
    epochs = [[snaps[0]]]
    for prev, cur in zip(snaps, snaps[1:]):
        common = set(prev["by_key"]) & set(cur["by_key"])
        changed = sum(1 for k in common
                      if (cur["by_key"][k].get("season_points") or 0)
                      != (prev["by_key"][k].get("season_points") or 0))
        if common and changed >= 0.5 * len(common):
            epochs.append([cur])
        else:
            epochs[-1].append(cur)

    rounds = []
    for i in range(1, len(epochs)):
        before, after = epochs[i - 1], epochs[i]
        rounds.append({"index": i, "date": after[0]["fetched_at"][:10],
                       "base": before[-1], "first": after[0], "last": after[-1]})

    history = defaultdict(list)   # key -> [ {round, points, price_before, price_after, delta} ]
    for r in rounds:
        for k, a_last in r["last"]["by_key"].items():
            b = r["base"]["by_key"].get(k)
            a_first = r["first"]["by_key"].get(k)
            if not b or not a_first:
                continue
            if a_first.get("season_points") is None or b.get("season_points") is None:
                continue
            pts = a_first["season_points"] - b["season_points"]
            pb, pa = b["price"], a_last["price"]
            history[k].append({"round": r["index"], "date": r["date"], "points": pts,
                               "price_before": pb, "price_after": pa,
                               "delta": round(pa - pb, 2), "ppm": pts / pb if pb else 0})
    return rounds, history


def tier_of(price, split):
    return "A" if price >= split else "B"


def learn_thresholds(history, window, split, min_samples):
    """Для кожної категорії: за якої метрики яка зміна ціни."""
    obs = defaultdict(list)      # tier -> [(metric, delta)]
    for rows in history.values():
        for i, row in enumerate(rows):
            if i + 1 < window:
                continue        # недостатньо попередніх етапів для повного вікна
            m = statistics.mean(x["ppm"] for x in rows[i + 1 - window:i + 1])
            obs[tier_of(row["price_before"], split)].append((m, row["delta"]))

    learned = {}
    for tier, pts in obs.items():
        groups = defaultdict(list)
        for m, d in pts:
            groups[d].append(m)
        deltas = sorted(groups)
        info = {"samples": len(pts),
                "groups": [{"delta": d, "n": len(groups[d]),
                            "min_metric": round(min(groups[d]), 3),
                            "max_metric": round(max(groups[d]), 3)} for d in deltas],
                "boundaries": [], "overlaps": 0}
        for lo, hi in zip(deltas, deltas[1:]):
            top_lo, bot_hi = max(groups[lo]), min(groups[hi])
            if top_lo > bot_hi:
                info["overlaps"] += sum(1 for m in groups[lo] if m > bot_hi) + \
                                    sum(1 for m in groups[hi] if m < top_lo)
            info["boundaries"].append({"between": [lo, hi],
                                       "metric": round((top_lo + bot_hi) / 2, 3)})
        info["usable"] = (len(pts) >= min_samples and len(deltas) >= 2
                          and info["overlaps"] <= 0.1 * len(pts))
        learned[tier] = info
    return learned


def rules_for_tier(tier, learned, assumed):
    """Повертає (межі метрики, дельти, джерело)."""
    lt = learned.get(tier)
    if lt and lt["usable"]:
        bounds = [b["metric"] for b in lt["boundaries"]]
        deltas = [g["delta"] for g in lt["groups"]]
        return bounds, deltas, "learned"
    return assumed["thresholds_ppm"], assumed["deltas"][tier], "assumed"


def needed_points(rows, price, window, bounds):
    """Скільки очок треба на наступному етапі, щоб метрика досягла кожної межі."""
    prev = [r["ppm"] for r in rows[-(window - 1):]] if window > 1 else []
    n = len(prev) + 1                    # якщо історії мало — вікно коротше
    s = sum(prev)
    return [round((b * n - s) * price, 1) for b in bounds]


def main():
    window = CONFIG["window"]
    assumed = CONFIG["assumed_rules"]
    split = assumed["tier_split_price"]
    snaps = load_snapshots()
    if not snaps:
        print("Немає знімків у data/snapshots — спочатку запусти collector/collect.py")
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps({"empty": True}), encoding="utf-8")
        return

    rounds, history = build_rounds(snaps)
    learned = learn_thresholds(history, window, split, CONFIG["min_samples_to_learn"])
    latest = snaps[-1]

    # ціна на кожному знімку — для графіка
    price_series = defaultdict(list)
    for s in snaps:
        for k, a in s["by_key"].items():
            ser = price_series[k]
            if not ser or ser[-1]["price"] != a["price"]:
                ser.append({"date": s["fetched_at"][:10], "price": a["price"]})

    assets = []
    for a in latest["assets"]:
        k = a["key"]
        rows = history.get(k, [])
        tier = tier_of(a["price"], split)
        bounds, deltas, src = rules_for_tier(tier, learned, assumed)
        need = needed_points(rows, a["price"], window, bounds)
        ser = price_series.get(k, [])
        assets.append({
            "key": k, "name": a["name"], "type": a.get("type"), "team": a.get("team"),
            "price": a["price"], "season_points": a.get("season_points"),
            "picked_pct": a.get("picked_pct"), "tier": tier,
            "last_delta": rows[-1]["delta"] if rows else None,
            "last_points": rows[-1]["points"] if rows else a.get("last_points"),
            "total_change": round(a["price"] - ser[0]["price"], 2) if ser else 0,
            "rounds": rows[-10:], "price_series": ser,
            "bands": [{"delta": d} for d in deltas],
            "needed": [{"for_delta": deltas[i + 1], "points": need[i]}
                       for i in range(len(need))],
            "rules_source": src,
            "calc": {"prev_ppm_sum": round(sum(r["ppm"] for r in rows[-(window - 1):]), 4)
                     if window > 1 else 0,
                     "n": min(len(rows), window - 1) + 1, "bounds": bounds,
                     "deltas": deltas},
        })

    out = {
        "generated_at": latest["fetched_at"],
        "snapshots": len(snaps), "rounds_tracked": len(rounds), "window": window,
        "tier_split_price": split, "learned": learned, "assumed": assumed,
        "assets": assets,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Знімків: {len(snaps)}, етапів відстежено: {len(rounds)}")
    for tier, info in sorted(learned.items()):
        print(f"\nКатегорія {tier}: спостережень {info['samples']}, "
              f"перетинів {info['overlaps']}, придатно: {info['usable']}")
        for g in info["groups"]:
            print(f"  зміна {g['delta']:+.1f}M: метрика {g['min_metric']:.3f}–"
                  f"{g['max_metric']:.3f} (n={g['n']})")
        for b in info["boundaries"]:
            print(f"  межа {b['between'][0]:+.1f}/{b['between'][1]:+.1f}: ≈{b['metric']}")
    print(f"\nЗаписано {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
