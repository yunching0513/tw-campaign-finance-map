#!/usr/bin/env python3
"""補齊立委候選人政黨（kiang 無 2020 立委候選人來源、2024 原住民/退選區域亦缺）。

來源：中文維基「YYYY年中華民國立法委員選舉區域暨原住民選舉區投票結果列表」。
該頁以中選會公布結果為準，逐選舉區列出所有候選人與政黨。

輸出 data/party/legislator_party.json：[{name, district, year, party}]，
  district＝縣市名 或「山地原住民」「平地原住民」；party 為原始黨名（交由 build 端 norm_party 正規化）。
  build_map_data.load_party_map 會把它併入 PartyMap，候選人姓名以精確＋模糊比對。
"""
import json
import re
import sys
from pathlib import Path

import requests

API = "https://zh.wikipedia.org/w/api.php"
# 維基媒體要求帶可識別的 User-Agent，否則回 403
UA = ("tw-campaign-finance-map/1.0 (civic open-data viz; "
      "contact: jtl0513@gmail.com) python-requests")
SUBPAGE = "{year}年中華民國立法委員選舉區域暨原住民選舉區投票結果列表"

# 政黨模板縮寫 → 黨名（其餘模板多本身即中文黨名，直接採用）
PARTY_TPL = {
    "dpp": "民主進步黨", "kmt": "中國國民黨", "npp": "時代力量",
    "tpp": "台灣民眾黨", "pfp": "親民黨", "nonp": "無黨籍", "np": "新黨",
    "gpt": "綠黨", "tsu": "台灣團結聯盟", "tsp": "台灣基進", "sdp": "社會民主黨",
    "mkt": "民國黨", "twp": "台灣維新", "nhsa": "台灣國民會議",
}

COUNTIES = ["臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市",
            "基隆市", "新竹市", "新竹縣", "苗栗縣", "彰化縣", "南投縣",
            "雲林縣", "嘉義市", "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣",
            "臺東縣", "澎湖縣", "金門縣", "連江縣"]

CELL_ATTR = re.compile(r'^\|\s*(?:[a-zA-Z-]+\s*=\s*"[^"]*"\s*\|)?(.*)$')
LINK = re.compile(r"\[\[([^\]]+)\]\]")
TPL = re.compile(r"\{\{\s*([^}|]+?)\s*\}\}")


def fetch_wikitext(title: str, cache: Path) -> str:
    fp = cache / ("wiki_" + title.replace("/", "_")[:60] + ".txt")
    if fp.exists():
        return fp.read_text(encoding="utf-8")
    r = requests.get(API, params={"action": "parse", "page": title,
                                  "prop": "wikitext", "format": "json",
                                  "formatversion": "2"},
                     headers={"User-Agent": UA}, timeout=120)
    r.raise_for_status()
    d = r.json()
    if "parse" not in d:
        raise RuntimeError(f"維基頁面取得失敗：{title} → {d.get('error')}")
    wt = d["parse"]["wikitext"]
    fp.write_text(wt, encoding="utf-8")
    return wt


def section_district(header: str):
    h = header.strip()
    if "平地原住民" in h:
        return "平地原住民"
    if "山地原住民" in h:
        return "山地原住民"
    return h if h in COUNTIES else None


def clean_name(cell: str):
    m = LINK.search(cell)
    raw = m.group(1) if m else cell
    raw = raw.split("|")[-1]                 # [[目標|顯示]] 取顯示
    raw = re.sub(r"\(.*?\)|（.*?）", "", raw)   # 去消歧義
    raw = raw.replace("'", "").replace("*", "").strip()
    raw = re.sub(r"\[\[File:.*", "", raw).strip()
    return raw


def cell_party(cell: str):
    m = TPL.search(cell)
    if m:
        tok = m.group(1).strip()
        return PARTY_TPL.get(tok.lower(), tok)
    m = LINK.search(cell)
    if m:
        return m.group(1).split("|")[-1].strip()
    return cell.strip()


def parse_subpage(wt: str, year: int):
    out, district = [], None
    rows, cur = [], []
    in_table = False

    def flush_row():
        # cur: 該列各 cell 的純值；欄序 號次,候選人,性別,政黨,...
        if district and len(cur) >= 4:
            name = clean_name(cur[1])
            party = cell_party(cur[3])
            if name and party and not name.isdigit():
                out.append({"name": name, "district": district,
                            "year": year, "party": party})

    head = re.compile(r"^(=+)\s*(.*?)\s*(=+)\s*$")
    for line in wt.splitlines():
        s = line.strip()
        hm = head.match(s)
        if hm and hm.group(1) == hm.group(3):
            # 縣市分佈在多層：直轄市於 L2、其餘縣市於「臺灣省／福建省」下 L3、
            # 原住民於「原住民」下 L3；選舉區子節沿用目前縣市，其餘節重置。
            name = re.sub(r"\[\[|\]\]", "", hm.group(2)).split("|")[-1].strip()
            d = section_district(name)        # 先判縣市／原住民（原住民節名含「選舉區」故須先判）
            if d:
                district = d
            elif not re.search(r"選舉?區", name):   # 既非縣市又非選區子節（選區/選舉區皆有）→ 重置
                district = None
            in_table = False
            continue
        if s.startswith("{|"):
            in_table = True
            cur = []
            continue
        if s.startswith("|}"):
            if cur:
                flush_row()
            in_table = False
            cur = []
            continue
        if not in_table:
            continue
        if s.startswith("|-"):
            if cur:
                flush_row()
            cur = []
            continue
        if s.startswith("!"):           # 表頭列，略過
            continue
        if s.startswith("|"):
            m = CELL_ATTR.match(s)
            cur.append(m.group(1).strip() if m else s[1:].strip())
    return out


def main():
    root = Path(__file__).resolve().parent
    cache = root / "data" / "party"
    cache.mkdir(parents=True, exist_ok=True)
    recs = []
    for year in (2020, 2024):
        try:
            wt = fetch_wikitext(SUBPAGE.format(year=year), cache)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {year} 立委維基取得失敗：{e}", file=sys.stderr)
            continue
        got = parse_subpage(wt, year)
        recs.extend(got)
        from collections import Counter
        bydist = Counter(r["district"] for r in got)
        print(f"  {year} 立委：解析 {len(got)} 位（{len(bydist)} 選區別）")
    out = cache / "legislator_party.json"
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"已輸出 {out}（共 {len(recs)} 筆）")


if __name__ == "__main__":
    main()
