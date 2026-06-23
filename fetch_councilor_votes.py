#!/usr/bin/env python3
"""補議員得票數（中選會 office CSV 無票數、維基為逐選區上百表格），供「選票 CP 值」用。

來源：kiang/db.cec.gov.tw 的逐村里檔 data/elections/2020-2024/*.json，
每檔含該村里的「2022議員」候選人陣列 {name, party, votes, elected}。
本腳本以 blobless sparse clone 只取該資料夾（約 38MB），再把每位候選人
跨村里的票數加總 → 每位議員候選人的總得票數。

輸出 data/party/councilor_votes.json：[{name, district(縣市), year:2022, votes, elected}]。
build_map_data.load_outcomes 會把票數併入 OutcomeMap（職位＝議員）。

注意：依（姓名, 縣市）歸戶，與獻金資料同口徑；同縣市同名不同選區者會合併（罕見）。
"""
import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO = "https://github.com/kiang/db.cec.gov.tw.git"
SUBDIR = "data/elections/2020-2024"


def sparse_clone(dest: Path):
    subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none",
                    "--depth", "1", REPO, str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", SUBDIR], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout"], check=True)


def aggregate(folder: Path):
    agg = defaultdict(lambda: {"votes": 0, "elected": False})
    for fp in folder.glob("*.json"):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        county = d.get("county", "")
        for c in d.get("2022議員", []) or []:
            nm = (c.get("name") or "").strip()
            if not nm:
                continue
            e = agg[(nm, county)]
            e["votes"] += int(c.get("votes") or 0)
            if c.get("elected"):
                e["elected"] = True
    return [{"name": k[0], "district": k[1], "year": 2022,
             "votes": v["votes"], "elected": v["elected"]}
            for k, v in agg.items() if v["votes"] > 0]


def main():
    out = Path(__file__).resolve().parent / "data" / "party" / "councilor_votes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="kiang_council_"))
    try:
        print("sparse clone（只取逐村里選舉檔，約 38MB）…")
        sparse_clone(tmp)
        recs = aggregate(tmp / SUBDIR)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not recs:
        print("  ! 未取得議員票數", file=sys.stderr)
        return
    out.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    print(f"已輸出 {out}（{len(recs)} 位議員候選人）")


if __name__ == "__main__":
    main()
