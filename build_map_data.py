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


def _topcorp(store, n_comp, n_recip):
    """store: {key: {name,id,total,recip{}}} → 前 n_comp 大企業金主清單。"""
    out = []
    for k, e in sorted(store.items(), key=lambda kv: -kv[1]["total"])[:n_comp]:
        recs = sorted(e["recip"].items(), key=lambda kv: -kv[1])[:n_recip]
        out.append({"key": k, "name": e["name"], "id": e["id"],
                    "total": round(e["total"]), "n_recip": len(e["recip"]),
                    "to": [{"name": n, "amount": round(a)} for n, a in recs]})
    return out


# 產業分類（依公司名稱關鍵字推測；資料本身無產業欄位）
LAND_DEV_KW = ["建設", "開發", "不動產", "地產", "房屋", "重劃", "都更", "建商",
               "土地", "資產"]
CONSTRUCTION_KW = ["營造", "建築", "土木", "營建", "工程", "包工", "預拌",
                   "混凝土", "鋼鐵", "水泥", "起重"]


def classify_industry(name: str):
    """回傳 (產業標籤, 是否土地開發相關)。"""
    n = name or ""
    if any(k in n for k in LAND_DEV_KW):
        return "建商地產", True
    if any(k in n for k in CONSTRUCTION_KW):
        return "營造工程", True
    return "其他", False


# ===== 候選人政黨（資料來源：g0v kiang/db.cec.gov.tw，整理自中選會）=====
COUNTIES = ["臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市", "基隆市",
            "新竹市", "嘉義市", "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
            "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣"]
PARTY_GROUP = {
    "民主進步黨": "民進黨", "中國國民黨": "國民黨",
    "台灣民眾黨": "民眾黨", "臺灣民眾黨": "民眾黨",
    "時代力量": "時代力量", "台灣基進": "台灣基進", "臺灣基進": "台灣基進",
    "親民黨": "親民黨", "新黨": "新黨", "台灣團結聯盟": "台聯",
}
CEC_RAW = "https://raw.githubusercontent.com/kiang/db.cec.gov.tw/master/data/"
CEC_2022_FILES = ["直轄市長", "縣市長", "直轄市議員", "縣市議員", "鄉鎮市長", "村里長"]


def norm_party(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    if "無黨" in p or "未經政黨" in p:
        return "無黨籍"
    return PARTY_GROUP.get(p, "其他")


def area_to_county(area: str) -> str:
    a = norm_county(area)
    for c in COUNTIES:
        if a.startswith(c):
            return c
    return a


def load_party_map(data_dir: Path) -> dict:
    """回傳 {(候選人, 縣市, 西元年): 政黨群組}。下載並快取於 data/party/。"""
    cache = data_dir / "party"
    cache.mkdir(parents=True, exist_ok=True)
    pmap: dict = {}

    # 2022 各職位候選人 CSV
    import csv as _csv
    import io as _io
    for office in CEC_2022_FILES:
        fp = cache / f"2022_{office}.csv"
        if not fp.exists():
            try:
                r = requests.get(CEC_RAW + f"2022/{office}.csv", timeout=60)
                r.raise_for_status()
                fp.write_bytes(r.content)
            except Exception as e:  # noqa: BLE001
                print(f"  ! 政黨資料下載失敗 2022/{office}: {e}", file=sys.stderr)
                continue
        for row in _csv.DictReader(_io.StringIO(fp.read_text(encoding="utf-8"))):
            nm = (row.get("cand_name") or "").strip()
            if nm:
                pmap[(nm, area_to_county(row.get("area", "")), 2022)] = \
                    norm_party(row.get("party", ""))

    # 2024 區域立委（從村里得票 JSON 萃取 候選人→政黨→選區縣市）
    fp = cache / "2024_zone_cunli.json"
    if not fp.exists():
        try:
            r = requests.get(CEC_RAW + "ly/2024_zone_cunli.json", timeout=120)
            r.raise_for_status()
            fp.write_bytes(r.content)
        except Exception as e:  # noqa: BLE001
            print(f"  ! 2024 立委政黨資料下載失敗: {e}", file=sys.stderr)
            fp = None
    if fp and fp.exists():
        zj = json.loads(fp.read_text(encoding="utf-8"))
        for v in zj.values():
            county = area_to_county(v.get("zone", ""))
            for c in (v.get("votes") or {}).values():
                nm = (c.get("name") or "").strip()
                if nm:
                    pmap[(nm, county, 2024)] = norm_party(c.get("party", ""))

    print(f"  政黨對照：{len(pmap)} 筆（2022 各職位 + 2024 區域立委）")
    return pmap


def build_candidate_registry(norm_dir: Path, party_map: dict, floor=50000, top_n=15):
    """每位候選人（依 姓名|職位|選區|年）的金主結構：
    收入總額、來源類別占比、企業金主之產業分布、收受最多的前 N 位金主。
    僅收錄收入 >= floor 的候選人，控制檔案大小。"""
    cands: dict[str, dict] = {}
    for layer in LAYERS:
        p = norm_dir / f"transactions_{layer['year']}.csv"
        if not p.exists():
            continue
        types, label, yr = set(layer["types"]), layer["label"], layer["year"] + 1911
        seen = set()
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["direction"] != "income" or r["election_type"] not in types:
                    continue
                cand = r["candidate"].strip()
                if not cand:
                    continue
                district = norm_county(r["electoral_district"])
                key = f"{cand}|{label}|{district}|{yr}"
                try:
                    amt = float(r["amount"] or 0)
                except ValueError:
                    amt = 0.0
                dt = donor_type(r["account_subject"])
                dname = (r["counterparty"] or "").strip()
                did = (r["counterparty_id"] or "").strip()
                dd = (key, dname, did, r["txn_date_roc"], r["amount"], r["account_subject"])
                if dd in seen:
                    continue
                seen.add(dd)
                c = cands.get(key)
                if not c:
                    c = {"name": cand, "office": label, "district": district, "year": yr,
                         "total": 0.0, "by_type": {t: 0.0 for t in DONOR_TYPES},
                         "corp_ind": {"建商地產": 0.0, "營造工程": 0.0, "其他": 0.0},
                         "donors": {}}
                    cands[key] = c
                c["total"] += amt
                c["by_type"][dt] += amt
                if dt == "營利事業":
                    ind, _ = classify_industry(dname)
                    c["corp_ind"][ind] += amt
                dk = (did if (did.isdigit() and len(did) == 8) else dname, dt)
                e = c["donors"].get(dk)
                if not e:
                    e = {"name": dname or ("匿名" if dt == "匿名" else "（未具名）"),
                         "type": dt,
                         "id": did if (did.isdigit() and len(did) == 8) else "",
                         "ind": classify_industry(dname)[0] if dt == "營利事業" else "",
                         "amt": 0.0}
                    c["donors"][dk] = e
                e["amt"] += amt

    out = {}
    for key, c in cands.items():
        if c["total"] < floor:
            continue
        donors = sorted(c["donors"].values(), key=lambda e: -e["amt"])[:top_n]
        out[key] = {"name": c["name"], "office": c["office"], "district": c["district"],
                    "year": c["year"], "total": round(c["total"]),
                    "party": party_map.get((c["name"], c["district"], c["year"]), ""),
                    "n_donors": len(c["donors"]),
                    "by_type": {t: round(v) for t, v in c["by_type"].items() if v},
                    "corp_ind": {k: round(v) for k, v in c["corp_ind"].items() if v},
                    "top_donors": [{"name": e["name"], "type": e["type"], "id": e["id"],
                                    "ind": e["ind"], "amount": round(e["amt"])}
                                   for e in donors]}
    return out


def build_company_registry(norm_dir: Path, party_map: dict):
    """跨選區/職位彙總每家企業的捐贈網絡（捐給哪些候選人）。
    僅收錄『捐給 2 位以上候選人』的企業（政商關係的重點），並標註產業。"""
    reg: dict[str, dict] = {}
    for layer in LAYERS:
        p = norm_dir / f"transactions_{layer['year']}.csv"
        if not p.exists():
            continue
        types, label, yr = set(layer["types"]), layer["label"], layer["year"] + 1911
        seen = set()
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["direction"] != "income" or r["election_type"] not in types:
                    continue
                if donor_type(r["account_subject"]) != "營利事業":
                    continue
                cid = (r["counterparty_id"] or "").strip()
                cname = (r["counterparty"] or "").strip()
                key = cid if (cid.isdigit() and len(cid) == 8) else cname
                cand = r["candidate"].strip()
                if not key or not cand:
                    continue
                county = norm_county(r["electoral_district"])
                try:
                    amt = float(r["amount"] or 0)
                except ValueError:
                    amt = 0.0
                dd = (key, cand, r["txn_date_roc"], r["amount"], label, county)
                if dd in seen:
                    continue
                seen.add(dd)
                e = reg.get(key)
                if not e:
                    ind, land = classify_industry(cname)
                    e = {"name": cname,
                         "id": cid if (cid.isdigit() and len(cid) == 8) else "",
                         "ind": ind, "land": land, "total": 0.0, "to": {}}
                    reg[key] = e
                if cname and not e["name"]:
                    e["name"] = cname
                    e["ind"], e["land"] = classify_industry(cname)
                e["total"] += amt
                tk = (cand, label, county, yr)
                e["to"][tk] = e["to"].get(tk, 0.0) + amt

    companies = []
    for key, e in reg.items():
        dons = sorted(
            ({"name": k[0], "office": k[1], "district": k[2], "year": k[3],
              "party": party_map.get((k[0], k[2], k[3]), ""),
              "amount": round(v)} for k, v in e["to"].items()),
            key=lambda d: -d["amount"])
        n_recip = len({d["name"] for d in dons})
        if n_recip < 2:           # 只收錄跨候選人金主（押寶多位才是政商關係重點）
            continue
        parties = sorted({d["party"] for d in dons if d["party"]})
        companies.append({"key": key, "name": e["name"], "id": e["id"],
                          "ind": e["ind"], "land": e["land"],
                          "total": round(e["total"]), "n_recip": n_recip,
                          "parties": parties, "to": dons[:60]})
    companies.sort(key=lambda c: (-c["n_recip"], -c["total"]))
    return companies


def build_county_corp(norm_dir: Path, top_n=40):
    """各縣市的企業（營利事業）金主排名，依捐贈總額排序（跨職位合計）。"""
    cc: dict[str, dict] = {}
    for layer in LAYERS:
        p = norm_dir / f"transactions_{layer['year']}.csv"
        if not p.exists():
            continue
        types = set(layer["types"])
        seen = set()
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["direction"] != "income" or r["election_type"] not in types:
                    continue
                if donor_type(r["account_subject"]) != "營利事業":
                    continue
                county = norm_county(r["electoral_district"])
                if not county or county in ("山地原住民", "平地原住民"):
                    continue
                cid = (r["counterparty_id"] or "").strip()
                cname = (r["counterparty"] or "").strip()
                key = cid if (cid.isdigit() and len(cid) == 8) else cname
                if not key:
                    continue
                cand = r["candidate"].strip()
                try:
                    amt = float(r["amount"] or 0)
                except ValueError:
                    amt = 0.0
                dd = (county, key, cand, r["txn_date_roc"], r["amount"])
                if dd in seen:
                    continue
                seen.add(dd)
                e = cc.setdefault(county, {}).get(key)
                if not e:
                    ind, land = classify_industry(cname)
                    e = {"key": key, "name": cname,
                         "id": cid if (cid.isdigit() and len(cid) == 8) else "",
                         "ind": ind, "land": land, "total": 0.0, "recips": set()}
                    cc[county][key] = e
                e["total"] += amt
                if cand:
                    e["recips"].add(cand)
    out = {}
    for county, store in cc.items():
        items = sorted(store.values(), key=lambda e: -e["total"])[:top_n]
        out[county] = [{"key": e["key"], "name": e["name"], "id": e["id"],
                        "ind": e["ind"], "land": e["land"], "total": round(e["total"]),
                        "n_recip": len(e["recips"])} for e in items]
    return out


def aggregate_layer(csv_path: Path, types: set[str]):
    """彙總某 CSV 中、屬於 types 的收入；回傳 (per_county, national)。
    以 (候選人,日期,對象,金額,科目) 去重，減輕更正/補申報重複計算。
    同時彙總「營利事業」捐贈者（企業金主）：以統編優先、否則公司名為鍵，
    記錄各企業捐贈總額與捐給哪些候選人。"""
    counties: dict[str, dict] = {}
    nat = {"total": 0.0, "by_type": {t: 0.0 for t in DONOR_TYPES}, "txn": 0,
           "cands": set(), "corp": {}}
    seen = set()

    def blank():
        return {"total": 0.0, "by_type": {t: 0.0 for t in DONOR_TYPES},
                "txn": 0, "cand_amt": {}, "corp": {}}

    def add_corp(store, cid, cname, amt, recip):
        key = cid if (cid.isdigit() and len(cid) == 8) else cname
        if not key:
            return
        e = store.get(key)
        if not e:
            e = {"name": cname, "id": cid if (cid.isdigit() and len(cid) == 8) else "",
                 "total": 0.0, "recip": {}}
            store[key] = e
        e["total"] += amt
        if cname and not e["name"]:
            e["name"] = cname
        if recip:
            e["recip"][recip] = e["recip"].get(recip, 0.0) + amt

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
            cid = (r["counterparty_id"] or "").strip()
            cname = (r["counterparty"] or "").strip()
            nat["total"] += amt
            nat["by_type"][dt] += amt
            nat["txn"] += 1
            if cand:
                nat["cands"].add((county, cand))
            if dt == "營利事業":
                add_corp(nat["corp"], cid, cname, amt, cand)
            if not county or county in ("山地原住民", "平地原住民"):
                continue  # 不分區/原住民 立委不落在縣市，計入全國但不上色
            c = counties.setdefault(county, blank())
            c["total"] += amt
            c["by_type"][dt] += amt
            c["txn"] += 1
            if cand:
                c["cand_amt"][cand] = c["cand_amt"].get(cand, 0.0) + amt
            if dt == "營利事業":
                add_corp(c["corp"], cid, cname, amt, cand)

    per_county = {}
    for county, c in counties.items():
        tops = sorted(c["cand_amt"].items(), key=lambda kv: -kv[1])[:6]
        per_county[county] = {
            "total": round(c["total"]),
            "by_type": {t: round(v) for t, v in c["by_type"].items()},
            "txn": c["txn"], "candidate_count": len(c["cand_amt"]),
            "top": [{"name": n, "amount": round(a)} for n, a in tops],
            "top_corp": _topcorp(c["corp"], 8, 2),
        }
    national = {"total": round(nat["total"]),
                "by_type": {t: round(v) for t, v in nat["by_type"].items()},
                "txn": nat["txn"], "candidate_count": len(nat["cands"]),
                "top_corp": _topcorp(nat["corp"], 30, 3)}
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
                                    "txn": 0, "candidate_count": 0, "top": [], "top_corp": []})
        national[layer["key"]] = nat
        offices.append({"key": layer["key"], "label": layer["label"],
                        "year_ad": layer["year"] + 1911, "types": layer["types"]})
        print(f"  {layer['label']}：全國 {nat['total']:,} 元、{nat['candidate_count']} 人")

    if not offices:
        sys.exit("沒有任何層級有資料，請先跑 ardata_scraper.py 產生 transactions_*.csv")

    print("載入候選人政黨對照（中選會／g0v）…")
    party_map = load_party_map(data_dir)

    print("建立企業金主跨選區網絡…")
    companies = build_company_registry(norm, party_map)
    land_n = sum(1 for c in companies if c["land"])
    print(f"  跨候選人企業金主：{len(companies)} 家（土地開發相關 {land_n} 家）")

    print("建立候選人金主結構…")
    candidates = build_candidate_registry(norm, party_map)
    matched = sum(1 for c in candidates.values() if c["party"])
    print(f"  候選人（收入≥5萬）：{len(candidates)} 位，對到政黨 {matched} 位")

    # 各職位 × 政黨 募款彙總
    psum: dict = {}
    for rec in candidates.values():
        pty = rec.get("party") or "未標示"
        s = psum.setdefault(rec["office"], {}).setdefault(
            pty, {"party": pty, "total": 0, "indiv": 0, "corp": 0, "n": 0})
        s["total"] += rec["total"]
        s["indiv"] += rec["by_type"].get("個人", 0)
        s["corp"] += rec["by_type"].get("營利事業", 0)
        s["n"] += 1
    party_summary = {off: sorted(d.values(), key=lambda x: -x["total"])
                     for off, d in psum.items()}

    payload = {
        "meta": {"viewbox": list(viewbox), "donor_types": DONOR_TYPES,
                 "offices": offices,
                 "source": "監察院政治獻金公開查閱平臺 ardata.cy.gov.tw"},
        "national": national,
        "counties": sorted(counties.values(),
                           key=lambda c: -c["layers"][offices[0]["key"]]["total"]),
        "companies": companies,
        "party_summary": party_summary,
        "county_corp": build_county_corp(norm),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.MAP_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    print(f"\n已輸出 {out}  ({out.stat().st_size/1024:.0f} KB)")

    cand_out = out.parent / "candidates.js"
    cand_out.write_text("window.CAND_DATA = " + json.dumps(candidates, ensure_ascii=False) + ";\n",
                        encoding="utf-8")
    print(f"已輸出 {cand_out}  ({cand_out.stat().st_size/1024:.0f} KB)")
    print("層級：" + " / ".join(f"{o['label']}({o['year_ad']})" for o in offices))


if __name__ == "__main__":
    main()
