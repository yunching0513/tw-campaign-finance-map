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
# 每個 (職位 × 年度) 一筆。map=True 者可上縣市地圖；總統為全國性，map=False。
def _L(office, office_label, year, types, mp=True):
    return {"key": f"{office}_{year+1911}", "office": office, "office_label": office_label,
            "year": year, "types": types, "map": mp}


LAYERS = [
    _L("mayor", "縣市長", 107, ["直轄市長", "縣市長"]),
    _L("mayor", "縣市長", 111, ["直轄市長", "縣市長"]),
    _L("council", "議員", 107, ["直轄市議員", "縣市議員"]),
    _L("council", "議員", 111, ["直轄市議員", "縣市議員"]),
    _L("township", "鄉鎮市長", 107, ["鄉鎮市長"]),
    _L("township", "鄉鎮市長", 111, ["鄉鎮市長"]),
    _L("village", "村里長", 107, ["村里長"]),
    _L("village", "村里長", 111, ["村里長"]),
    _L("legislator", "立法委員", 109, ["立法委員"]),
    _L("legislator", "立法委員", 113, ["立法委員"]),
    _L("president", "總統", 105, ["總統副總統"], mp=False),
    _L("president", "總統", 109, ["總統副總統"], mp=False),
    _L("president", "總統", 113, ["總統副總統"], mp=False),
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


# 幾何簡化：地圖以 ~520px 寬呈現，海岸線原始精度遠超螢幕所需。
# Douglas–Peucker 抽點 + 丟棄肉眼看不到的小島礁，可把幾何資料壓掉 ~80%。
SIMPLIFY_EPS = 0.7   # 容差（投影後座標單位，約等於像素）
MIN_RING_PX = 1.6    # 投影後外接框最大邊小於此值的環直接丟棄


def _dp_open(pts, eps):
    """開放多段線的 Douglas–Peucker 簡化（迭代版，避免遞迴深度問題）。"""
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        ax, ay = pts[s]
        bx, by = pts[e]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        dmax, idx = -1.0, -1
        for i in range(s + 1, e):
            px, py = pts[i]
            d = abs((px - ax) * dy - (py - ay) * dx) / L
            if d > dmax:
                dmax, idx = d, i
        if dmax > eps and idx != -1:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [p for p, k in zip(pts, keep) if k]


def _dp(pts, eps):
    """簡化一個環。閉合環（首=尾）若直接做 DP，基準線退化為一點會把整環壓成兩點，
    故先取離起點最遠的頂點當第二錨點，拆成兩段開放線各自簡化。"""
    n = len(pts)
    if n < 4:
        return pts
    if pts[0] == pts[-1]:
        ax, ay = pts[0]
        far = max(range(1, n - 1),
                  key=lambda i: (pts[i][0] - ax) ** 2 + (pts[i][1] - ay) ** 2)
        a = _dp_open(pts[:far + 1], eps)
        b = _dp_open(pts[far:], eps)
        return a[:-1] + b   # a 結尾與 b 開頭同為 far，去重後接續
    return _dp_open(pts, eps)


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
        rxs = [p[0] for p in pts]
        rys = [p[1] for p in pts]
        if max(max(rxs) - min(rxs), max(rys) - min(rys)) < MIN_RING_PX:
            continue  # 海上小島礁，螢幕上看不到
        pts = _dp(pts, SIMPLIFY_EPS)
        if len(pts) < 3:
            continue
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


# 產業分類（依公司名稱關鍵字推測；資料本身無產業欄位，僅供參考）
# 順序＝優先序：較專一的關鍵字在前，避免被通用詞先吃掉。
INDUSTRY_RULES = [
    ("建商地產", ["建設", "開發", "不動產", "地產", "房屋", "重劃", "都更", "住宅",
                  "仲介", "建商"]),
    ("營造工程", ["營造", "建築", "土木", "營建", "包工", "預拌", "混凝土", "鋼構",
                  "起重", "拆除", "水電", "工程"]),
    ("醫療生技", ["製藥", "生技", "生醫", "醫療", "醫院", "藥品", "藥業", "藥廠",
                  "診所", "長照", "醫材", "保健"]),
    ("科技電子", ["半導體", "光電", "電子", "通訊", "網路", "資訊", "軟體", "系統",
                  "電機", "精密", "數位", "科技"]),
    ("金融投資", ["投資", "控股", "銀行", "信用合作社", "保險", "證券", "期貨",
                  "資產", "創投", "租賃", "融資", "金控", "信託", "財務"]),
    ("媒體廣告", ["廣告", "傳播", "媒體", "出版", "影視", "娛樂", "印刷", "公關",
                  "行銷", "文化", "設計"]),
    ("餐飲旅宿", ["餐飲", "餐廳", "飯店", "酒店", "旅館", "旅行", "觀光", "美食"]),
    ("批發貿易", ["百貨", "超市", "商場", "購物", "零售", "批發", "貿易", "進出口",
                  "量販"]),
    ("運輸物流", ["運輸", "物流", "貨運", "航運", "海運", "空運", "倉儲", "通運",
                  "快遞", "交通"]),
    ("能源環保", ["能源", "電力", "瓦斯", "石油", "天然氣", "綠能", "太陽能",
                  "風力", "環保", "回收"]),
    ("農林漁牧", ["農產", "農業", "林業", "漁業", "畜", "養殖", "牧場"]),
    ("製造工業", ["工業", "製造", "機械", "化工", "化學", "塑膠", "紡織", "食品",
                  "飲料", "金屬", "電線", "電纜", "鋼鐵", "鋁業", "鐵線", "玻璃",
                  "橡膠", "水泥"]),
]
LAND_INDS = {"建商地產", "營造工程"}


def classify_industry(name: str):
    """回傳 (產業標籤, 是否土地開發相關)。（依公司名稱關鍵字）"""
    n = name or ""
    for label, kws in INDUSTRY_RULES:
        if any(k in n for k in kws):
            return label, label in LAND_INDS
    return "其他", False


# ── 財政部「主要行業名稱」→ 產業桶（比公司名稱精準，優先採用）──
FIA_MAP: dict = {}
FIA_RULES = [
    ("建商地產", ["不動產", "建設", "住宅營建", "土地開發", "都市更新", "重劃", "房地產"]),
    ("營造工程", ["營造", "土木", "建築工程", "裝潢", "水電工程", "管道工程", "鋼構",
                  "模板", "拆除", "預拌混凝土", "專門營造", "機電工程", "配管", "防水",
                  "鋪面", "油漆", "工程"]),
    ("金融投資", ["投資", "證券", "銀行", "信用合作", "保險", "融資", "期貨", "控股",
                  "信託", "基金", "票券", "當鋪", "資產管理"]),
    ("醫療生技", ["製藥", "藥品", "生物科技", "生技", "醫療", "醫院", "診所", "長照",
                  "醫療器材", "保健", "藥粧"]),
    ("科技電子", ["電子", "半導體", "積體電路", "資訊", "軟體", "電腦", "通訊", "光電",
                  "網際網路", "資料處理", "電信", "電機", "數據"]),
    ("媒體廣告", ["廣告", "傳播", "出版", "印刷", "影片", "影視", "電影", "廣播",
                  "娛樂", "設計", "公關", "攝影", "唱片", "媒體"]),
    ("餐飲旅宿", ["餐廳", "餐飲", "飲料店", "小吃", "旅館", "飯店", "旅行", "觀光",
                  "民宿", "咖啡"]),
    ("批發貿易", ["批發", "零售", "百貨", "超市", "量販", "經紀", "貿易", "進出口", "五金"]),
    ("運輸物流", ["貨運", "運輸", "物流", "倉儲", "航運", "海運", "空運", "快遞",
                  "客運", "通運", "停車場"]),
    ("能源環保", ["電力", "發電", "瓦斯", "石油", "汽油", "加油", "天然氣", "能源",
                  "太陽能", "風力", "廢棄物", "回收", "污染", "環境工程", "清潔"]),
    ("農林漁牧", ["農產", "農業", "種植", "畜牧", "養殖", "漁業", "林業", "畜"]),
    ("製造工業", ["製造", "工業", "機械", "化工", "化學", "塑膠", "紡織", "食品",
                  "飲料", "金屬", "鋼", "鋁", "鐵", "玻璃", "橡膠", "水泥", "加工"]),
]


def load_fia_map(data_dir: Path):
    global FIA_MAP
    p = data_dir / "party" / "ban_industry.json"
    if p.exists():
        FIA_MAP = json.loads(p.read_text(encoding="utf-8"))
        print(f"  財政部行業對照：{len(FIA_MAP)} 筆（精準分類）")
    else:
        print("  （無 ban_industry.json，產業改用公司名稱推測；可跑 fetch_industry.py）")
    return FIA_MAP


def map_fia_industry(name: str) -> str:
    n = name or ""
    for label, kws in FIA_RULES:
        if any(k in n for k in kws):
            return label
    return "其他"


def industry_of(name: str, ban: str):
    """優先用財政部主要行業（統編對到）；否則退回公司名稱關鍵字。"""
    if ban and ban in FIA_MAP:
        lab = map_fia_industry(FIA_MAP[ban])
        return lab, lab in LAND_INDS
    return classify_industry(name)


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
CEC_OFFICE_FILES = ["直轄市長", "縣市長", "直轄市議員", "縣市議員", "鄉鎮市長", "村里長"]
CEC_NINEINONE_YEARS = [2018, 2022]   # kiang 有逐職位候選人 CSV 的九合一年度
# 總統候選人政黨（少數、公開週知，硬編；區域立委 2020 無乾淨候選人來源故從缺）
PRESIDENT_PARTY = {
    "蔡英文": "民進黨", "賴清德": "民進黨",
    "朱立倫": "國民黨", "韓國瑜": "國民黨", "侯友宜": "國民黨",
    "宋楚瑜": "親民黨", "柯文哲": "民眾黨",
}


def norm_party(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    # 只把「真・無黨籍」歸為無黨籍；「無黨團結聯盟」是政黨，勿誤併
    if "未經政黨" in p or p == "無黨籍":
        return "無黨籍"
    return PARTY_GROUP.get(p, "其他")


def area_to_county(area: str) -> str:
    a = norm_county(area)
    for c in COUNTIES:
        if a.startswith(c):
            return c
    return a


# 原住民候選人在獻金資料與中選會名單常以「族名＋漢名」不同順序、不同分隔符登錄
# （例：獻金「Mulas‧Ismahasan陳慧君」↔ 中選會「陳慧君 Mulas．Ismahasan」）。
# 故除精確比對外，於「同縣市同年」範圍內再以「去分隔符」與「純漢字姓名相等」回退比對，
# 且僅在比對結果政黨唯一時才採用，避免誤判。
_NAME_SEP = " ·‧．・•∙. ‧．・"


def _strip_sep(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch not in _NAME_SEP)


def _han(s: str) -> str:
    return "".join(ch for ch in (s or "") if "一" <= ch <= "鿿")


class PartyMap(dict):
    """(姓名, 縣市, 年)→政黨；附同縣市同年索引，支援原住民等姓名變體的模糊回退。"""

    def __init__(self):
        super().__init__()
        self.idx = {}     # (縣市, 年) -> [(中選會姓名, 政黨), ...]
        self.byname = {}  # (純漢字姓名, 年) -> {政黨, ...}（全國唯一時可跨選區回退）

    def add(self, name, county, year, party):
        self[(name, county, year)] = party
        self.idx.setdefault((county, year), []).append((name, party))
        if party:
            self.byname.setdefault((_han(name), year), set()).add(party)

    def lookup(self, name, county, year):
        p = self.get((name, county, year))
        if p:
            return p
        cands = self.idx.get((county, year))
        if not cands:
            return ""
        nkey = _strip_sep(name)                       # 去分隔符後相等
        for cn, cp in cands:
            if cp and _strip_sep(cn) == nkey:
                return cp
        h = _han(name)                                # 純漢字姓名相等且政黨唯一（同縣市同年）
        if len(h) >= 2:
            hits = {cp for cn, cp in cands if cp and _han(cn) == h}
            if len(hits) == 1:
                return next(iter(hits))
        # 跨選區回退：姓名（≥3 漢字，降低同名風險）全國該年僅對應一個政黨時才採用
        # 用於原住民立委等「獻金選區＝家鄉縣市、實際選區＝原住民」的情形
        if len(h) >= 3:
            g = self.byname.get((h, year))
            if g and len(g) == 1:
                return next(iter(g))
        return ""


class OutcomeMap:
    """(姓名, 選區, 年, 職位)→{elected, votes}；沿用模糊比對處理姓名變體。
    含職位是因 2022 九合一同時有縣市長與議員，僅靠姓名+縣市+年會跨職位互撞。"""

    def __init__(self):
        self.exact = {}
        self.idx = {}

    def add(self, name, district, year, office, elected, votes):
        v = {"elected": bool(elected), "votes": votes}
        self.exact[(name, district, year, office)] = v
        self.idx.setdefault((district, year, office), []).append((name, v))

    def lookup(self, name, district, year, office):
        v = self.exact.get((name, district, year, office))
        if v:
            return v
        cands = self.idx.get((district, year, office))
        if not cands:
            return None
        nkey = _strip_sep(name)
        for cn, cv in cands:
            if _strip_sep(cn) == nkey:
                return cv
        h = _han(name)
        if len(h) >= 2:
            hits = [cv for cn, cv in cands if _han(cn) == h]
            if len(hits) == 1:
                return hits[0]
        return None


# CEC 職位檔名 → 候選人登錄職位（用於對當選結果）
OUTCOME_OFFICE = {"直轄市長": "縣市長", "縣市長": "縣市長",
                  "直轄市議員": "議員", "縣市議員": "議員",
                  "鄉鎮市長": "鄉鎮市長", "村里長": "村里長"}


def load_outcomes(data_dir: Path) -> OutcomeMap:
    """當選與否＋得票數。九合一取自中選會 office CSV 的 is_victor（無票數）；
    立委取自維基（含票數）。回傳 OutcomeMap。"""
    import csv as _csv
    import io as _io
    cache = data_dir / "party"
    om = OutcomeMap()
    for yr in CEC_NINEINONE_YEARS:
        for office in CEC_OFFICE_FILES:
            fp = cache / f"{yr}_{office}.csv"
            if not fp.exists():
                continue
            for row in _csv.DictReader(_io.StringIO(fp.read_text(encoding="utf-8"))):
                nm = (row.get("cand_name") or "").strip()
                if nm:
                    om.add(nm, area_to_county(row.get("area", "")), yr,
                           OUTCOME_OFFICE.get(office, office),
                           row.get("is_victor") == "Y", None)
    lp = cache / "legislator_party.json"
    if lp.exists():
        for rec in json.loads(lp.read_text(encoding="utf-8")):
            om.add(rec["name"], rec["district"], rec["year"], "立法委員",
                   rec.get("elected", False), rec.get("votes"))

    def _merge_votes(fname, office, label):
        fp = cache / fname
        if not fp.exists():
            return
        upd = 0
        for rec in json.loads(fp.read_text(encoding="utf-8")):
            ent = om.lookup(rec["name"], rec["district"], rec["year"], office)
            if ent and ent.get("votes") is None:
                ent["votes"] = rec["votes"]
                upd += 1
            elif not ent:
                om.add(rec["name"], rec["district"], rec["year"], office,
                       rec.get("elected", False), rec["votes"])
                upd += 1
        print(f"  {label}得票數補充：{upd} 筆")

    _merge_votes("mayor_votes.json", "縣市長", "縣市長")        # 維基，目前僅 2022
    _merge_votes("councilor_votes.json", "議員", "議員")        # kiang 逐村里彙總，2022
    print(f"  選舉結果（當選/票數）：{len(om.exact)} 筆")
    return om


def load_party_map(data_dir: Path) -> PartyMap:
    """回傳 PartyMap：(候選人, 縣市, 西元年)→政黨群組。下載並快取於 data/party/。"""
    cache = data_dir / "party"
    cache.mkdir(parents=True, exist_ok=True)
    pmap = PartyMap()

    # 九合一各職位候選人 CSV（2018、2022）
    import csv as _csv
    import io as _io
    for yr in CEC_NINEINONE_YEARS:
        for office in CEC_OFFICE_FILES:
            fp = cache / f"{yr}_{office}.csv"
            if not fp.exists():
                try:
                    r = requests.get(CEC_RAW + f"{yr}/{office}.csv", timeout=60)
                    r.raise_for_status()
                    fp.write_bytes(r.content)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! 政黨資料下載失敗 {yr}/{office}: {e}", file=sys.stderr)
                    continue
            for row in _csv.DictReader(_io.StringIO(fp.read_text(encoding="utf-8"))):
                nm = (row.get("cand_name") or "").strip()
                if nm:
                    pmap.add(nm, area_to_county(row.get("area", "")), yr,
                             norm_party(row.get("party", "")))

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
                    pmap.add(nm, county, 2024, norm_party(c.get("party", "")))

    # 立委候選人政黨（補 kiang 沒有的 2020 全部、2024 原住民/退選區域）
    # 來源：維基「YYYY年立委選舉區域暨原住民選舉區投票結果列表」，由 fetch_legislator_party.py 產生。
    # 不覆蓋已存在的鍵（保留中選會 zone_cunli 的 2024 區域），僅補空缺。
    lp = cache / "legislator_party.json"
    if lp.exists():
        added = 0
        for rec in json.loads(lp.read_text(encoding="utf-8")):
            key = (rec["name"], rec["district"], rec["year"])
            if key not in pmap:
                pmap.add(rec["name"], rec["district"], rec["year"],
                         norm_party(rec["party"]))
                added += 1
        print(f"  立委政黨補充（維基）：+{added} 筆")
    else:
        print("  ! 缺 data/party/legislator_party.json，請先跑 fetch_legislator_party.py", file=sys.stderr)

    # 總統候選人（硬編；district 在候選人登錄中為「全國」）
    for yr in (2016, 2020, 2024):
        for nm, p in PRESIDENT_PARTY.items():
            pmap.add(nm, "全國", yr, p)

    print(f"  政黨對照：{len(pmap)} 筆（2018/2022 各職位 + 立委 + 總統）")
    return pmap


def build_candidate_registry(norm_dir: Path, party_map: dict, outcome_map=None,
                             floor=50000, top_n=15):
    """每位候選人（依 姓名|職位|選區|年）的金主結構：
    收入總額、來源類別占比、企業金主之產業分布、收受最多的前 N 位金主。
    僅收錄收入 >= floor 的候選人，控制檔案大小。"""
    cands: dict[str, dict] = {}
    for layer in LAYERS:
        p = norm_dir / f"transactions_{layer['year']}.csv"
        if not p.exists():
            continue
        types, label, yr = set(layer["types"]), layer["office_label"], layer["year"] + 1911
        seen = set()
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["direction"] != "income" or r["election_type"] not in types:
                    continue
                cand = r["candidate"].strip()
                if not cand:
                    continue
                district = norm_county(r["electoral_district"]) or "全國"
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
                         "corp_ind": {}, "donors": {}}
                    cands[key] = c
                c["total"] += amt
                c["by_type"][dt] += amt
                if dt == "營利事業":
                    ind, _ = industry_of(dname, did)
                    c["corp_ind"][ind] = c["corp_ind"].get(ind, 0.0) + amt
                dk = (did if (did.isdigit() and len(did) == 8) else dname, dt)
                e = c["donors"].get(dk)
                if not e:
                    e = {"name": dname or ("匿名" if dt == "匿名" else "（未具名）"),
                         "type": dt,
                         "id": did if (did.isdigit() and len(did) == 8) else "",
                         "ind": industry_of(dname, did)[0] if dt == "營利事業" else "",
                         "amt": 0.0}
                    c["donors"][dk] = e
                e["amt"] += amt

    out = {}
    for key, c in cands.items():
        if c["total"] < floor:
            continue
        donors = sorted(c["donors"].values(), key=lambda e: -e["amt"])[:top_n]
        rec = {"name": c["name"], "office": c["office"], "district": c["district"],
               "year": c["year"], "total": round(c["total"]),
               "party": party_map.lookup(c["name"], c["district"], c["year"]),
               "n_donors": len(c["donors"]),
               "by_type": {t: round(v) for t, v in c["by_type"].items() if v},
               "corp_ind": {k: round(v) for k, v in c["corp_ind"].items() if v},
               "top_donors": [{"name": e["name"], "type": e["type"], "id": e["id"],
                               "ind": e["ind"], "amount": round(e["amt"])}
                              for e in donors]}
        oc = outcome_map.lookup(c["name"], c["district"], c["year"], c["office"]) if outcome_map else None
        if oc:
            rec["elected"] = oc["elected"]
            if oc["votes"] is not None:
                rec["votes"] = oc["votes"]
        out[key] = rec
    return out


def build_company_registry(norm_dir: Path, party_map: dict):
    """跨選區/職位彙總每家企業的捐贈網絡（捐給哪些候選人）。
    僅收錄『捐給 2 位以上候選人』的企業（政商關係的重點），並標註產業。"""
    reg: dict[str, dict] = {}
    for layer in LAYERS:
        p = norm_dir / f"transactions_{layer['year']}.csv"
        if not p.exists():
            continue
        types, label, yr = set(layer["types"]), layer["office_label"], layer["year"] + 1911
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
                county = norm_county(r["electoral_district"]) or "全國"
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
                    ind, land = industry_of(cname, cid)
                    e = {"name": cname,
                         "id": cid if (cid.isdigit() and len(cid) == 8) else "",
                         "ind": ind, "land": land, "total": 0.0, "to": {}}
                    reg[key] = e
                if cname and not e["name"]:
                    e["name"] = cname
                    e["ind"], e["land"] = industry_of(cname, cid)
                e["total"] += amt
                tk = (cand, label, county, yr)
                e["to"][tk] = e["to"].get(tk, 0.0) + amt

    companies = []
    for key, e in reg.items():
        dons = sorted(
            ({"name": k[0], "office": k[1], "district": k[2], "year": k[3],
              "party": party_map.lookup(k[0], k[2], k[3]),
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
                    ind, land = industry_of(cname, cid)
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


def build_industry_totals(norm_dir: Path):
    """各產業（營利事業）捐贈總額，依職位（含全部）彙總。
    回傳 {scope: [{ind, total, companies, txn} 依總額排序]}。"""
    data: dict = {}

    def add(scope, ind, amt, comp):
        s = data.setdefault(scope, {}).setdefault(
            ind, {"total": 0.0, "companies": set(), "txn": 0})
        s["total"] += amt
        s["txn"] += 1
        if comp:
            s["companies"].add(comp)

    for layer in LAYERS:
        p = norm_dir / f"transactions_{layer['year']}.csv"
        if not p.exists():
            continue
        types = set(layer["types"])
        scope = f"{layer['office_label']}·{layer['year'] + 1911}"
        seen = set()
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["direction"] != "income" or r["election_type"] not in types:
                    continue
                if donor_type(r["account_subject"]) != "營利事業":
                    continue
                cid = (r["counterparty_id"] or "").strip()
                cname = (r["counterparty"] or "").strip()
                comp = cid if (cid.isdigit() and len(cid) == 8) else cname
                dd = (scope, comp, r["candidate"], r["txn_date_roc"], r["amount"])
                if dd in seen:
                    continue
                seen.add(dd)
                try:
                    amt = float(r["amount"] or 0)
                except ValueError:
                    amt = 0.0
                ind = industry_of(cname, cid)[0]
                add("全部", ind, amt, comp)
                add(scope, ind, amt, comp)

    out = {}
    for scope, d in data.items():
        rows = [{"ind": k, "total": round(v["total"]),
                 "companies": len(v["companies"]), "txn": v["txn"]}
                for k, v in d.items()]
        rows.sort(key=lambda x: -x["total"])
        out[scope] = rows
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

    national, office_meta = {}, {}
    for layer in LAYERS:
        if not layer["map"]:
            continue   # 總統為全國性，不上縣市地圖
        csv_path = norm / f"transactions_{layer['year']}.csv"
        if not csv_path.exists():
            print(f"  ! 略過 {layer['key']}：缺 {csv_path.name}")
            continue
        per_county, nat = aggregate_layer(csv_path, set(layer["types"]))
        if nat["total"] == 0:
            print(f"  ! {layer['key']} 無資料，略過")
            continue
        for name, c in counties.items():
            c["layers"][layer["key"]] = per_county.get(name) or {
                "total": 0, "by_type": {t: 0 for t in DONOR_TYPES},
                "txn": 0, "candidate_count": 0, "top": [], "top_corp": []}
        national[layer["key"]] = nat
        om = office_meta.setdefault(layer["office"], {
            "key": layer["office"], "label": layer["office_label"], "years": []})
        om["years"].append(layer["year"] + 1911)
        print(f"  地圖 {layer['key']}：全國 {nat['total']:,} 元、{nat['candidate_count']} 人")

    offices = list(office_meta.values())
    for o in offices:
        o["years"] = sorted(set(o["years"]))
    if not offices:
        sys.exit("沒有任何層級有資料，請先跑 ardata_scraper.py 產生 transactions_*.csv")
    default_key = f"{offices[0]['key']}_{offices[0]['years'][-1]}"

    print("載入財政部行業對照…")
    load_fia_map(data_dir)

    print("載入候選人政黨對照（中選會／g0v）…")
    party_map = load_party_map(data_dir)
    outcome_map = load_outcomes(data_dir)

    print("建立企業金主跨選區網絡…")
    companies = build_company_registry(norm, party_map)
    land_n = sum(1 for c in companies if c["land"])
    print(f"  跨候選人企業金主：{len(companies)} 家（土地開發相關 {land_n} 家）")

    print("建立候選人金主結構…")
    candidates = build_candidate_registry(norm, party_map, outcome_map)
    matched = sum(1 for c in candidates.values() if c["party"])
    elected_n = sum(1 for c in candidates.values() if c.get("elected"))
    votes_n = sum(1 for c in candidates.values() if c.get("votes") is not None)
    print(f"  其中標記當選 {elected_n} 位、有得票數 {votes_n} 位")
    print(f"  候選人（收入≥5萬）：{len(candidates)} 位，對到政黨 {matched} 位")

    # 各職位 × 政黨 募款彙總
    psum: dict = {}
    for rec in candidates.values():
        pty = rec.get("party") or "未標示"
        s = psum.setdefault(f"{rec['office']}·{rec['year']}", {}).setdefault(
            pty, {"party": pty, "total": 0, "indiv": 0, "corp": 0, "n": 0})
        s["total"] += rec["total"]
        s["indiv"] += rec["by_type"].get("個人", 0)
        s["corp"] += rec["by_type"].get("營利事業", 0)
        s["n"] += 1
    party_summary = {off: sorted(d.values(), key=lambda x: -x["total"])
                     for off, d in psum.items()}

    # 政黨輪廓（各職位＋全部）：來源組成、企業產業組成、平均每人
    pprof: dict = {}

    def _addpp(scope, rec):
        pty = rec.get("party") or "未標示"
        s = pprof.setdefault(scope, {}).setdefault(
            pty, {"total": 0, "n": 0, "by_type": {}, "corp_ind": {}})
        s["total"] += rec["total"]
        s["n"] += 1
        for t, v in rec["by_type"].items():
            s["by_type"][t] = s["by_type"].get(t, 0) + v
        for k, v in rec["corp_ind"].items():
            s["corp_ind"][k] = s["corp_ind"].get(k, 0) + v
    for rec in candidates.values():
        _addpp("全部", rec)
        _addpp(f"{rec['office']}·{rec['year']}", rec)

    # core（隨地圖立即載入）：不含 companies；companies/candidates 改為前端延遲載入。
    payload = {
        "meta": {"viewbox": list(viewbox), "donor_types": DONOR_TYPES,
                 "offices": offices,
                 "source": "監察院政治獻金公開查閱平臺 ardata.cy.gov.tw"},
        "national": national,
        "counties": sorted(counties.values(),
                           key=lambda c: -c["layers"].get(default_key, {}).get("total", 0)),
        "party_summary": party_summary,
        "party_profile": pprof,
        "industry_totals": build_industry_totals(norm),
        "county_corp": build_county_corp(norm),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.MAP_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    print(f"\n已輸出 {out}  ({out.stat().st_size/1024:.0f} KB)")

    comp_out = out.parent / "companies.js"
    comp_out.write_text("window.COMPANIES_DATA = " + json.dumps(companies, ensure_ascii=False) + ";\n",
                        encoding="utf-8")
    print(f"已輸出 {comp_out}  ({comp_out.stat().st_size/1024:.0f} KB)  [延遲載入]")

    cand_out = out.parent / "candidates.js"
    cand_out.write_text("window.CAND_DATA = " + json.dumps(candidates, ensure_ascii=False) + ";\n",
                        encoding="utf-8")
    print(f"已輸出 {cand_out}  ({cand_out.stat().st_size/1024:.0f} KB)  [延遲載入]")
    print("地圖職位：" + " / ".join(f"{o['label']}{o['years']}" for o in offices))


if __name__ == "__main__":
    main()
