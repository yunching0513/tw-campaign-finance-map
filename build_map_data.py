#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_map_data.py
=================
為「臺灣地圖政治獻金視覺化」雛形產生資料：
  1. 下載臺灣縣市 GeoJSON（含 fallback 來源），快取於 data/tw_counties.geojson
  2. 將經緯度投影成 SVG 路徑（等距圓柱投影 + 緯度修正），離島(金門/連江)做 inset
  3. 彙總 transactions_{roc}.csv 的「收入(捐贈)」資料：
       每縣市：總收入、各來源類別(個人/營利事業/政黨/人民團體/匿名/其他)、
               候選人數、收受金額前幾名候選人
  4. 輸出 web/map_data.js  (window.MAP_DATA = {...})，供 index.html 以 <script> 載入
     （採 .js 而非 .json，方便直接 file:// 開啟，免本機伺服器）

用法：
  python3 build_map_data.py --year 111            # 2022 九合一
  python3 build_map_data.py --year 111 --year2 113 # 同時帶入 2024 做對照(可選)
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

# GeoJSON 可能的縣市名稱屬性鍵
NAME_KEYS = ["COUNTYNAME", "countyname", "name", "C_Name", "NAME_2", "T_Name"]

# 視覺化只把這些類別當「市長層級」头条（其餘仍計入總額）
MAYOR_TYPES = {"直轄市長", "縣市長"}


def norm_county(s: str) -> str:
    """統一縣市名：台->臺，去空白。"""
    return (s or "").strip().replace("台", "臺")


# --------------------------------------------------------------------------- #
# 1. 取得 GeoJSON
# --------------------------------------------------------------------------- #
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
                cache.write_text(json.dumps(gj, ensure_ascii=False),
                                 encoding="utf-8")
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
    # 退而求其次：抓第一個含「縣/市」的字串值
    for v in props.values():
        if isinstance(v, str) and ("縣" in v or "市" in v):
            return norm_county(v)
    return ""


# --------------------------------------------------------------------------- #
# 2. 投影：經緯度 -> SVG 座標
# --------------------------------------------------------------------------- #
def iter_rings(geom: dict):
    """yield 每個 ring（座標串）。支援 Polygon / MultiPolygon。"""
    t = geom.get("type")
    coords = geom.get("coordinates", [])
    if t == "Polygon":
        for ring in coords:
            yield ring
    elif t == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                yield ring


def build_projection(features, width, height, pad):
    """回傳 (project_fn, viewbox) ；主島正常投影，金門/連江做左下 inset。"""
    lat0 = 23.7
    k = math.cos(math.radians(lat0))

    def raw(lon, lat):
        return lon * k, -lat  # 等距圓柱 + 緯度修正，y 反向

    # 主島 bbox（排除離島，避免地圖被拉很寬）
    main_pts = []
    for f in features:
        nm = feature_name(f.get("properties", {}))
        if nm in ("金門縣", "連江縣"):
            continue
        for ring in iter_rings(f.get("geometry", {})):
            for lon, lat in ring:
                main_pts.append(raw(lon, lat))
    xs = [p[0] for p in main_pts]
    ys = [p[1] for p in main_pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sx = (width - 2 * pad) / (maxx - minx)
    sy = (height - 2 * pad) / (maxy - miny)
    s = min(sx, sy)
    # 置中位移
    offx = pad + ((width - 2 * pad) - (maxx - minx) * s) / 2
    offy = pad + ((height - 2 * pad) - (maxy - miny) * s) / 2

    def project_main(lon, lat):
        x, y = raw(lon, lat)
        return (offx + (x - minx) * s, offy + (y - miny) * s)

    # 離島 inset：把金門、連江各自縮放平移到左下角小框
    inset_cfg = {
        "金門縣": (pad + 6, height - 86),
        "連江縣": (pad + 6, height - 150),
    }

    def project(lon, lat, county):
        if county in inset_cfg:
            # 以該縣自身 bbox 局部投影到 ~44px 小框
            return None  # 由 path_for_feature 處理（需該縣 bbox）
        return project_main(lon, lat)

    return project_main, inset_cfg, (0, 0, width, height)


def county_bbox(feature):
    pts = []
    for ring in iter_rings(feature.get("geometry", {})):
        pts.extend(ring)
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def path_for_feature(feature, project_main, inset_cfg, name) -> str:
    """產生 SVG path d。離島用 inset 局部投影。"""
    parts = []
    if name in inset_cfg:
        ox, oy = inset_cfg[name]
        minlon, minlat, maxlon, maxlat = county_bbox(feature)
        box = 40.0
        span = max(maxlon - minlon, maxlat - minlat) or 1e-6
        sc = box / span

        def proj(lon, lat):
            return (ox + (lon - minlon) * sc, oy - (lat - minlat) * sc)
    else:
        def proj(lon, lat):
            return project_main(lon, lat)

    for ring in iter_rings(feature.get("geometry", {})):
        if len(ring) < 3:
            continue
        pts = [proj(lon, lat) for lon, lat in ring]
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z"
        parts.append(d)
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 3. 彙總獻金資料
# --------------------------------------------------------------------------- #
def aggregate(csv_path: Path):
    """回傳 {county: {...}} 與全國彙總。"""
    counties: dict[str, dict] = {}
    nat = {"total": 0.0, "by_type": {t: 0.0 for t in DONOR_TYPES},
           "txn": 0, "candidates": set()}

    def blank():
        return {"total": 0.0, "by_type": {t: 0.0 for t in DONOR_TYPES},
                "txn": 0, "cand_amt": {}}

    with csv_path.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["direction"] != "income":
                continue
            county = norm_county(r["electoral_district"])
            if not county or county in ("山地原住民", "平地原住民"):
                continue
            try:
                amt = float(r["amount"] or 0)
            except ValueError:
                amt = 0.0
            dt = r["donor_type"] if r["donor_type"] in DONOR_TYPES else "其他"
            c = counties.setdefault(county, blank())
            c["total"] += amt
            c["by_type"][dt] += amt
            c["txn"] += 1
            cand = r["candidate"].strip()
            if cand:
                key = (cand, r["election_type"])
                c["cand_amt"][key] = c["cand_amt"].get(key, 0.0) + amt
            nat["total"] += amt
            nat["by_type"][dt] += amt
            nat["txn"] += 1
            if cand:
                nat["candidates"].add((county, cand, r["election_type"]))

    # 整理每縣市 top candidates
    out = {}
    for county, c in counties.items():
        tops = sorted(c["cand_amt"].items(), key=lambda kv: -kv[1])[:6]
        out[county] = {
            "total": round(c["total"]),
            "by_type": {t: round(v) for t, v in c["by_type"].items()},
            "txn": c["txn"],
            "candidate_count": len({k[0] for k in c["cand_amt"]}),
            "top": [{"name": n, "type": t, "amount": round(a)}
                    for (n, t), a in tops],
        }
    nat_out = {
        "total": round(nat["total"]),
        "by_type": {t: round(v) for t, v in nat["by_type"].items()},
        "txn": nat["txn"],
        "candidate_count": len(nat["candidates"]),
    }
    return out, nat_out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=111, help="主年度(民國)")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="docs/map_data.js")
    ap.add_argument("--width", type=float, default=520)
    ap.add_argument("--height", type=float, default=720)
    ap.add_argument("--pad", type=float, default=24)
    args = ap.parse_args()

    data_dir = Path(args.data)
    csv_path = data_dir / "normalized" / f"transactions_{args.year}.csv"
    if not csv_path.exists():
        sys.exit(f"找不到 {csv_path}，請先執行 ardata_scraper.py")

    print("讀取 GeoJSON…")
    gj = load_geojson(data_dir / "tw_counties.geojson")
    feats = gj["features"]

    print("建立投影…")
    project_main, inset_cfg, viewbox = build_projection(
        feats, args.width, args.height, args.pad)

    print(f"彙總獻金資料：{csv_path.name}")
    agg, nat = aggregate(csv_path)

    counties = []
    matched = 0
    for f in feats:
        name = feature_name(f.get("properties", {}))
        if not name:
            continue
        d = path_for_feature(f, project_main, inset_cfg, name)
        rec = {"name": name, "path": d}
        if name in agg:
            rec.update(agg[name])
            matched += 1
        else:
            rec.update({"total": 0, "by_type": {t: 0 for t in DONOR_TYPES},
                        "txn": 0, "candidate_count": 0, "top": []})
        counties.append(rec)

    print(f"  geojson 縣市：{len(counties)}，與獻金資料對上：{matched}")
    unmatched = sorted(set(agg) - {c["name"] for c in counties})
    if unmatched:
        print(f"  ! 獻金資料中未對應到地圖的縣市：{unmatched}")

    payload = {
        "meta": {"year_roc": args.year, "year_ad": args.year + 1911,
                 "viewbox": list(viewbox), "donor_types": DONOR_TYPES,
                 "source": "監察院政治獻金公開查閱平臺 ardata.cy.gov.tw"},
        "national": nat,
        "counties": sorted(counties, key=lambda c: -c["total"]),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.MAP_DATA = " +
                   json.dumps(payload, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"已輸出 {out}  ({kb:.0f} KB)")
    print(f"全國 {args.year+1911} 總捐贈收入：{nat['total']:,} 元"
          f"，{nat['candidate_count']} 位候選人，{nat['txn']:,} 筆")


if __name__ == "__main__":
    main()
