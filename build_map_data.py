#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_map_data.py  (v2 — 支援職位層級：縣市長 / 議員 / 立委 …)
=============================================================
為「臺灣地圖政治獻金視覺化」雛形產生資料：
  1. 下載臺灣縣市 GeoJSON（含 fallback），快取於 data/tw_counties.geojson
  2. 將經緯度投影成 SVG 路徑（等距圓柱 + 緯度修正），離島(金門/連江)做 inset
  3. 依「職位層級」彙總各縣市的捐贈收入：
       - 縣市長  (2022, 直轄市長+縣市長)
       - 議員    (2022, 直轄市議員+縣市議員)
       - 立法委員(2024)
       - 鄉鎮市長(2022)
       - 村里長  (2022)
     每層級 × 每縣市：總收入、來源組成(個人/企業/…)、候選人數、收受前六名
  4. 輸出 web/map_data.js → 改名部署後為 docs/map_data.js

需要 transactions_111.csv (2022) 與 transactions_113.csv (2024)。
缺其中一個年度，對應層級會自動略過並提示。

用法：
  python3 build_map_data.py                 # 預設讀 2022+2024，輸出 docs/map_data.js
  python3 build_map_data.py --out web/map_data.js   # 部署前在 web/ 預覽用
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("需要 requests：pip install requests")

csv.field_size_limit(10_000_000)

GEOJSON_SOURCES = [
    "https://raw.githubusercontent.com/g0v/twgeojson/master/json/twCounty2010.geo.json",
    "https://raw.githubusercontent.com/ronnywang/twgeojson/master/json/twCounty2010merge.geo.json",
    "https://raw.githubusercontent.com/codeforjapan/tw-county-geojson/master/twCounty2010.geo.json",
]
DONOR_TYPES = ["個人", "營利事業", "政黨", "人民團體", "匿名", "其他"]
NAME_KEYS = ["COUNTYNAME", "countyname", "name", "C_Name", "NAME_2", "T_Name"]

# 職位層級：UI 上可切換的圖層。type 對應 transactions 的 election_type。
LAYERS = [
    {"key": "mayor",      "label": "縣市長", "year": 111,
     "types": ["直轄市長", "縣市長"]},
    {"key": "council",    "label": "議員",   "year": 111,
     "types": ["直轄市議員", "縣市議員"]},
    {"key": "legislator", "label": "立法委員", "year": 113,
     "types": ["立法委員"]},
    {"key": "township",   "label": "鄉鎮市長", "year": 111,
     "types": ["鄉鎮市長"]},
    {"key": "village",    "label": "村里長", "year": 111,
     "types": ["村里長"]},
]


# GeoJSON 為 2010 年邊界，桃園當時仍是「桃園縣」；資料用升格後的「桃園市」
COUNTY_ALIASES = {"桃園縣": "桃園市"}


def norm_county(s: str) -> str:
    s = (s or "").strip().replace("台", "臺")
    return COUNTY_ALIASES.get(s, s)


# ---------------- GeoJSON ----------------
def load_geojson(cache: Path) -> dict:
    if cache.exists() and cache.stat().st_size > 1000:
        return json.loads(cache.read_text(encoding="utf-8"))
    last = None
    for url in GEOJSON_SOURCES:
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            gj = r.json()
            if gj.get("type") == "FeatureCollection" and gj.get("features"):
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
                print(f"  GeoJSON 來源：{url}  ({len(gj['features'])} features)")
                return gj
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  ! 來源失敗 {url}: {e}", file=sys.stderr)
    sys.exit(f"無法取得臺灣縣市 GeoJSON：{last}")


def feature_name(props: dict) -> str:
    for k in NAME_KEYS:
        if k in props and props[k]:
            return norm_county(str(props[k]))
    for v in props.values():
        if isinstance(v, str) and ("縣" in v or "市" in v):
            return norm_county(v)
    return ""


# ---------------- 投影 ----------------
def iter_rings(geom: dict):
    t, coords = geom.get("type"), geom.get("coordinates", [])
    if t == "Polygon":
        yield from coords
    elif t == "MultiPolygon":
        for poly in coords:
            yield from poly


def build_projection(features, width, height, pad):
    lat0 = 23.7
    k = math.cos(math.radians(lat0))

    def raw(lon, lat):
        return lon * k, -lat

    main_pts = []
    for f in features:
        if feature_name(f.get("properties", {})) in ("金門縣", "連江縣"):
            continue
        for ring in iter_rings(f.get("geometry", {})):
            main_pts.extend(raw(lon, lat) for lon, lat in ring)
    xs = [p[0] for p in main_pts]
    ys = [p[1] for p in main_pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    s = min((width - 2 * pad) / (maxx - minx), (height - 2 * pad) / (maxy - miny))
    offx = pad + ((width - 2 * pad) - (maxx - minx) * s) / 2
    offy = pad + ((height - 2 * pad) - (maxy - miny) * s) / 2

    def project_main(lon, lat):
        x, y = raw(lon, lat)
        return offx + (x - minx) * s, offy + (y - miny) * s

    inset_cfg = {"金門縣": (pad + 6, height - 86), "連江縣": (pad + 6, height - 150)}
    return project_main, inset_cfg, (0, 0, width, height)


def county_bbox(feature):
    pts = [p for ring in iter_rings(feature.get("geometry", {})) for p in ring]
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def path_for_feature(feature, project_main, inset_cfg, name) -> str:
    if name in inset_cfg:
        ox, oy = inset_cfg[name]
        minlon, minlat, maxlon, maxlat = county_bbox(feature)
        sc = 40.0 / (max(maxlon - minlon, maxlat - minlat) or 1e-6)

        def proj(lon, lat):
            return ox + (lon - minlon) * sc, oy - (lat - minlat) * sc
    else:
        proj = project_main

    parts = []
    for ring in iter_rings(feature.get("geometry", {})):
        if len(ring) < 3:
            continue
        pts = [proj(lon, lat) for lon, lat in ring]
        parts.append("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
    return " ".join(parts)


# ---------------- 彙總（依層級） ----------------
def donor_type(account_subject: str) -> str:
    for kw in DONOR_TYPES:
        if kw != "其他" and kw in (account_subject or ""):
            return kw
    return "其他"


def aggregate_layer(csv_path: Path, types: set[str]):
    """彙總某 CSV 中、屬於 types 的收入；回傳 (per_county, national)。
    以 (候選人,日期,對象,金額,科目) 去除完全重複列，減輕更正/補申報重複計算。"""
    counties: dict[str, dict] = {}
    nat = {"total": 0.0, "by_type": {t: 0.0 for t in DONOR_TYPES}, "txn": 0,
           "cands": set()}
    seen = set()

    def blank():
        return {"total": 0.0, "by_type": {t: 0.0 for t in DONOR_TYPES},
                "txn": 0, "cand_amt": {}}

    with csv_path.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["direction"] != "income" or r["election_type"] not in types:
                continue
            try:
                amt = float(r["amount"] or 0)
            except ValueError:
                amt = 0.0
            dedup = (r["candidate"], r["txn_date_roc"], r["counterparty"],
                     r["amount"], r["account_subject"])
            if dedup in seen:
                continue
            seen.add(dedup)
            dt = donor_type(r["account_subject"])
            cand = r["candidate"].strip()
            county = norm_county(r["electoral_district"])
            nat["total"] += amt
            nat["by_type"][dt] += amt
            nat["txn"] += 1
            if cand:
                nat["cands"].add((county, cand))
            if not county or county in ("山地原住民", "平地原住民"):
                continue  # 不分區/原住民 立委不落在縣市，計入全國但不上色
            c = counties.setdefault(county, blank())
            c["total"] += amt
            c["by_type"][dt] += amt
            c["txn"] += 1
            if cand:
                c["cand_amt"][cand] = c["cand_amt"].get(cand, 0.0) + amt

    per_county = {}
    for county, c in counties.items():
        tops = sorted(c["cand_amt"].items(), key=lambda kv: -kv[1])[:6]
        per_county[county] = {
            "total": round(c["total"]),
            "by_type": {t: round(v) for t, v in c["by_type"].items()},
            "txn": c["txn"], "candidate_count": len(c["cand_amt"]),
            "top": [{"name": n, "amount": round(a)} for n, a in tops],
        }
    national = {"total": round(nat["total"]),
                "by_type": {t: round(v) for t, v in nat["by_type"].items()},
                "txn": nat["txn"], "candidate_count": len(nat["cands"])}
    return per_county, national


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="docs/map_data.js")
    ap.add_argument("--width", type=float, default=520)
    ap.add_argument("--height", type=float, default=720)
    ap.add_argument("--pad", type=float, default=24)
    args = ap.parse_args()

    data_dir = Path(args.data)
    norm = data_dir / "normalized"

    print("讀取 GeoJSON…")
    gj = load_geojson(data_dir / "tw_counties.geojson")
    feats = gj["features"]
    print("建立投影…")
    project_main, inset_cfg, viewbox = build_projection(feats, args.width, args.height, args.pad)

    # 先建立縣市骨架（含 path）
    counties = {}
    for f in feats:
        name = feature_name(f.get("properties", {}))
        if not name:
            continue
        counties[name] = {"name": name,
                          "path": path_for_feature(f, project_main, inset_cfg, name),
                          "layers": {}}

    offices, national = [], {}
    for layer in LAYERS:
        csv_path = norm / f"transactions_{layer['year']}.csv"
        if not csv_path.exists():
            print(f"  ! 略過層級「{layer['label']}」：缺 {csv_path.name}")
            continue
        print(f"彙總層級：{layer['label']}（{layer['year']+1911}）…")
        per_county, nat = aggregate_layer(csv_path, set(layer["types"]))
        if nat["total"] == 0:
            print(f"  ! 層級「{layer['label']}」無資料，略過")
            continue
        for name, agg in per_county.items():
            if name in counties:
                counties[name]["layers"][layer["key"]] = agg
        # 縣市若該層級無資料，補 0 以利上色
        for name, c in counties.items():
            c["layers"].setdefault(layer["key"],
                                   {"total": 0, "by_type": {t: 0 for t in DONOR_TYPES},
                                    "txn": 0, "candidate_count": 0, "top": []})
        national[layer["key"]] = nat
        offices.append({"key": layer["key"], "label": layer["label"],
                        "year_ad": layer["year"] + 1911, "types": layer["types"]})
        print(f"  {layer['label']}：全國 {nat['total']:,} 元、{nat['candidate_count']} 人")

    if not offices:
        sys.exit("沒有任何層級有資料，請先跑 ardata_scraper.py 產生 transactions_*.csv")

    payload = {
        "meta": {"viewbox": list(viewbox), "donor_types": DONOR_TYPES,
                 "offices": offices,
                 "source": "監察院政治獻金公開查閱平臺 ardata.cy.gov.tw"},
        "national": national,
        "counties": sorted(counties.values(),
                           key=lambda c: -c["layers"][offices[0]["key"]]["total"]),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.MAP_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    print(f"\n已輸出 {out}  ({out.stat().st_size/1024:.0f} KB)")
    print("層級：" + " / ".join(f"{o['label']}({o['year_ad']})" for o in offices))


if __name__ == "__main__":
    main()
