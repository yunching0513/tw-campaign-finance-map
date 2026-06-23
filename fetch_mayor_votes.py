#!/usr/bin/env python3
"""補縣市長得票數（中選會 office CSV 無票數），供「選票 CP 值」用。

來源：中文維基「YYYY年中華民國直轄市長及縣市長選舉」，各縣市選舉結果表。
輸出 data/party/mayor_votes.json：[{name, district(縣市), year, votes, elected}]。
build_map_data.load_outcomes 會把票數併入既有 OutcomeMap（當選與否仍以中選會為準）。
"""
import json
import re
import sys
from pathlib import Path

import requests

API = "https://zh.wikipedia.org/w/api.php"
UA = ("tw-campaign-finance-map/1.0 (civic open-data viz; "
      "contact: jtl0513@gmail.com) python-requests")
PAGE = "{year}年中華民國直轄市長及縣市長選舉"

COUNTIES = ["臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市",
            "基隆市", "新竹市", "新竹縣", "苗栗縣", "彰化縣", "南投縣",
            "雲林縣", "嘉義市", "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣",
            "臺東縣", "澎湖縣", "金門縣", "連江縣"]

LINK = re.compile(r"\[\[([^\]]+)\]\]")
HEAD = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$")


def fetch_wikitext(title, cache):
    fp = cache / ("wiki_" + title.replace("/", "_")[:60] + ".txt")
    if fp.exists():
        return fp.read_text(encoding="utf-8")
    r = requests.get(API, params={"action": "parse", "page": title, "prop": "wikitext",
                                  "format": "json", "formatversion": "2"},
                     headers={"User-Agent": UA}, timeout=120)
    r.raise_for_status()
    d = r.json()
    if "parse" not in d:
        raise RuntimeError(f"維基取得失敗：{title} → {d.get('error')}")
    wt = d["parse"]["wikitext"]
    fp.write_text(wt, encoding="utf-8")
    return wt


def cellval(s):
    s = s[1:] if s[:1] in "|!" else s
    m = re.match(r'^\s*(?:[\w-]+\s*=\s*"[^"]*"\s*)+\|(.*)$', s)   # 去掉一個以上屬性
    return (m.group(1) if m else s).strip()


def clean_name(cell):
    m = LINK.search(cell)
    raw = m.group(1).split("|")[-1] if m else cell
    raw = re.sub(r"\(.*?\)|（.*?）", "", raw)
    return raw.replace("'", "").replace("*", "").strip()


def parse_page(wt, year):
    out, county, in_t, hdr, cur = [], None, False, "", []

    def flush():
        if not (county and "得票" in hdr and len(cur) >= 3):
            return
        name = clean_name(cur[1]) if len(cur) > 1 else ""
        # 得票數＝「得票率(%)」那格的前一格；取其「顯示數字」（最後一段逗號數字，
        # 避開 data-sort-value 等屬性殘留造成的天文數字）
        votes = None
        pct = next((i for i, c in enumerate(cur) if "%" in c), None)
        if pct and pct >= 1:
            nums = re.findall(r"\d[\d,]*", cur[pct - 1])
            if nums:
                votes = int(nums[-1].replace(",", ""))
        elected = any("Vote1" in c for c in cur)
        if name and votes and not name.isdigit():
            out.append({"name": name, "district": county, "year": year,
                        "votes": votes, "elected": elected})

    for line in wt.splitlines():
        s = line.strip()
        hm = HEAD.match(s)
        if hm:
            nm = re.sub(r"\[\[|\]\]", "", hm.group(2)).split("|")[-1].strip()
            if nm in COUNTIES:
                county = nm
            in_t = False
            continue
        if s.startswith("{|"):
            in_t, hdr, cur = True, "", []
            continue
        if s.startswith("|}"):
            flush()
            in_t = False
            continue
        if not in_t:
            continue
        if s.startswith("!"):
            hdr += s
            continue
        if s.startswith("|-"):
            flush()
            cur = []
            continue
        if s.startswith("|"):
            cur.append(cellval(s))
    return out


def main():
    cache = Path(__file__).resolve().parent / "data" / "party"
    cache.mkdir(parents=True, exist_ok=True)
    recs = []
    for year in (2018, 2022):
        try:
            wt = fetch_wikitext(PAGE.format(year=year), cache)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {year} 縣市長維基取得失敗：{e}", file=sys.stderr)
            continue
        got = parse_page(wt, year)
        # 去重（同人同縣市同年可能於多表重複；取最大票數）
        best = {}
        for r in got:
            k = (r["name"], r["district"], r["year"])
            if k not in best or r["votes"] > best[k]["votes"]:
                best[k] = r
        recs.extend(best.values())
        print(f"  {year} 縣市長：{len(best)} 位有票數（{len(set(r['district'] for r in best.values()))} 縣市）")
    (cache / "mayor_votes.json").write_text(
        json.dumps(recs, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"已輸出 data/party/mayor_votes.json（共 {len(recs)} 筆）")


if __name__ == "__main__":
    main()
