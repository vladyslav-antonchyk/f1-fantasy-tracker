"""Збирає поточні ціни й очки з F1 Fantasy.

Як працює:
1. Відкриває сторінки статистики гри в headless-браузері (Playwright).
2. Перехоплює ВСІ JSON-відповіді, які сайт завантажує, і зберігає їх у
   data/raw/<дата>/ разом зі списком URL (discovered_endpoints.json) —
   так ми автоматично "знаходимо API".
3. Шукає в цих JSON пілотів і команди з цінами (див. extract.py).
4. Пише знімок у data/snapshots/<дата-час>.json, якщо дані змінились.

Запуск:  python collector/collect.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from extract import find_assets, merge_assets  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA = ROOT / "data"
SKIP_URL = re.compile(r"(google|doubleclick|facebook|analytics|onetrust|cookielaw|"
                      r"hotjar|adobe|demdex|omtrdc|tiktok|twitter|consent)", re.I)


def hint_for(url: str) -> str | None:
    if "tab=driver" in url:
        return "driver"
    if "tab=constructor" in url:
        return "constructor"
    return None


async def accept_cookies(page):
    for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept all')",
                "button:has-text('Accept All')", "button:has-text('Accept')",
                "button:has-text('I agree')"]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(1000)
                return
        except Exception:
            pass


async def run() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H%MZ")
    raw_dir = DATA / "raw" / stamp
    raw_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = ROOT / "debug"
    debug_dir.mkdir(exist_ok=True)

    captured: list[dict] = []
    endpoints: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
            viewport={"width": 1400, "height": 1000}, locale="en-GB")
        page = await ctx.new_page()
        current_hint = {"v": None}

        async def on_response(resp):
            url = resp.url
            if SKIP_URL.search(url):
                return
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype and not url.split("?")[0].endswith(".json"):
                return
            try:
                body = await resp.json()
            except Exception:
                return
            text = json.dumps(body, ensure_ascii=False)
            h = hashlib.sha1(url.encode()).hexdigest()[:10]
            fname = f"{len(captured):03d}_{h}.json"
            (raw_dir / fname).write_text(text, encoding="utf-8")
            captured.append({"url": url, "file": fname, "body": body,
                             "hint": current_hint["v"]})
            endpoints.append({"url": url, "status": resp.status, "bytes": len(text),
                              "file": fname})

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        pages = sys.argv[1:] or CONFIG["pages"]   # можна передати свої URL для тесту
        for i, url in enumerate(pages):
            current_hint["v"] = hint_for(url)
            print(f"→ {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"  ! не відкрилась: {e}")
                continue
            await accept_cookies(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            for _ in range(6):                       # прокрутка, щоб довантажились списки
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(800)
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(debug_dir / f"page_{i}.png"), full_page=True)
            (debug_dir / f"page_{i}.txt").write_text(
                await page.inner_text("body"), encoding="utf-8")

        await browser.close()

    (raw_dir / "discovered_endpoints.json").write_text(
        json.dumps(endpoints, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Перехоплено JSON-відповідей: {len(captured)}")

    candidates = []
    for c in captured:
        for f in find_assets(c["body"], c["hint"]):
            f["url"] = c["url"]
            candidates.append(f)
    report = [{"url": c["url"], "path": c["path"], "count": len(c["assets"]),
               "mapping": c["mapping"]} for c in candidates]
    (raw_dir / "detected_lists.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for r in report:
        print(f"  ✓ {r['count']:>3} гравців у {r['url']} {r['path']}  поля={r['mapping']}")

    assets = merge_assets(candidates)
    if not assets:
        print("✗ Не знайшов жодного списку гравців з цінами. Дивись "
              f"{raw_dir}/discovered_endpoints.json і debug/*.png", file=sys.stderr)
        return 1

    n_drv = sum(a.get("type") == "driver" for a in assets)
    n_con = sum(a.get("type") == "constructor" for a in assets)
    print(f"Знайдено: {len(assets)} (пілотів {n_drv}, команд {n_con})")

    snap_dir = DATA / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    sig = json.dumps(sorted((a["key"], a["price"], a.get("season_points"))
                            for a in assets))
    prev = sorted(snap_dir.glob("*.json"))
    if prev:
        last = json.loads(prev[-1].read_text(encoding="utf-8"))
        last_sig = json.dumps(sorted((a["key"], a["price"], a.get("season_points"))
                                     for a in last["assets"]))
        if last_sig == sig:
            print("Дані не змінились з останнього знімка — новий не пишу.")
            return 0

    snap = {"fetched_at": now.isoformat(), "sources": sorted({c["url"] for c in candidates}),
            "assets": assets}
    (snap_dir / f"{stamp}.json").write_text(
        json.dumps(snap, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Збережено знімок data/snapshots/{stamp}.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
