#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_industry.py
=================
用「財政部全國營業（稅籍）登記資料集」的『主要行業名稱』精準標註企業金主產業，
取代以公司名稱關鍵字推測的做法。

流程：
  1. 從 data/normalized/transactions_*.csv 取出所有「營利事業」捐贈者的統一編號。
  2. 下載財政部大檔 BGMOPEN1.zip（約 66MB，解開後 ~306MB CSV，UTF-8）。
  3. 串流比對，僅保留我們需要的統編 → 主要行業名稱。
  4. 輸出 data/party/ban_industry.json （供 build_map_data.py 讀取）。

資料來源：政府資料開放平臺「全國營業(稅籍)登記資料集」(dataset 9400)
          https://eip.fia.gov.tw/data/BGMOPEN1.zip
（僅含營業中之營業人；歷史歇業資料未納入。）

用法：python3 fetch_industry.py   （偶爾更新一次即可）
"""
from __future__ import annotations
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("需要 requests：pip install requests")

csv.field_size_limit(10_000_000)
FIA_URL = "https://eip.fia.gov.tw/data/BGMOPEN1.zip"


def main():
    root = Path(__file__).parent
    norm = root / "data" / "normalized"
    out = root / "data" / "party" / "ban_industry.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    # 1) 需要查的統編
    need = set()
    for p in norm.glob("transactions_*.csv"):
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["direction"] == "income" and r["donor_type"] == "營利事業":
                    cid = (r["counterparty_id"] or "").strip()
                    if cid.isdigit() and len(cid) == 8:
                        need.add(cid)
    print(f"需要查詢的企業統編：{len(need)}")
    if not need:
        sys.exit("找不到統編，請先跑 ardata_scraper.py")

    # 2) 下載財政部大檔
    cache_zip = out.parent / "BGMOPEN1.zip"
    if not (cache_zip.exists() and cache_zip.stat().st_size > 60_000_000):
        print(f"下載 {FIA_URL} …（約 66MB，可能較慢）")
        with requests.get(FIA_URL, stream=True, timeout=600) as r:
            r.raise_for_status()
            with cache_zip.open("wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
    print(f"檔案大小：{cache_zip.stat().st_size:,} bytes")

    # 3) 串流比對
    found = {}
    with zipfile.ZipFile(cache_zip) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            for row in reader:
                ban = (row.get("統一編號") or "").strip()
                if ban in need and ban not in found:
                    found[ban] = (row.get("名稱") or "").strip()   # 主要行業名稱
    print(f"對到 {len(found)}（{len(found)/len(need)*100:.0f}%）")

    # 4) 輸出
    out.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    print(f"已輸出 {out}")


if __name__ == "__main__":
    main()
