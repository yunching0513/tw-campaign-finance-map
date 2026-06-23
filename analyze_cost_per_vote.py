#!/usr/bin/env python3
"""每票成本（政治獻金÷得票數）的政黨效果分析：排名 + OLS 迴歸。

問題：「在哪個黨」是否影響每票成本？還是只是因為某黨多選小而便宜的選區？
做法：被解釋變數取 ln(每票成本)（右偏，取對數較常態、係數可解讀為百分比差異），
控制「職位、是否當選、選區規模 ln(得票數)」後，看各黨 dummy 係數。
參照組＝國民黨、立法委員。穩健標準誤 HC1。資料＝有票數且有獻金之候選人。
僅供描述性參考：此為觀察資料，黨籍係數是相關非因果（混雜選區競爭度、現任、空戰/陸戰風格等）。
"""
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DUMMY_MIN = 20          # 樣本 >= 此數的政黨單獨設 dummy，其餘併「其他小黨」
REF_PARTY = "國民黨"
OFFICES = ["立法委員", "議員", "縣市長"]   # 立法委員為參照
REF_OFFICE = "立法委員"


def load():
    raw = (ROOT / "docs" / "candidates.js").read_text(encoding="utf-8")
    C = json.loads(raw[raw.index("=") + 1:].rstrip().rstrip(";"))
    rows = []
    for d in C.values():
        if d.get("votes") and d["total"] > 0 and d["office"] in OFFICES and (d.get("party") or ""):
            rows.append({"party": d["party"], "office": d["office"],
                         "elected": 1 if d.get("elected") else 0,
                         "votes": d["votes"], "cpv": d["total"] / d["votes"]})
    return rows


def _med(a):
    return float(np.median(a)) if len(a) else 0.0


def ranking(rows):
    print("\n========== 排名：各黨每票成本（元/票） ==========")
    for scope, pred in [("全部候選人", lambda r: True), ("僅當選者", lambda r: r["elected"])]:
        g = defaultdict(list)
        for r in rows:
            if pred(r):
                g[r["party"]].append(r["cpv"])
        items = [(p, _med(v), float(np.mean(v)), len(v)) for p, v in g.items() if len(v) >= 5]
        items.sort(key=lambda x: x[1])
        print(f"\n-- {scope}（n>=5 的黨；由便宜到貴）--")
        print(f"   {'政黨':<6}{'中位數':>8}{'平均':>8}{'人數':>7}")
        for p, md, mn, n in items:
            print(f"   {p:<6}{md:>8.0f}{mn:>8.0f}{n:>7}")


def ols(rows):
    parties = defaultdict(int)
    for r in rows:
        parties[r["party"]] += 1
    dummy_parties = [p for p in parties if parties[p] >= DUMMY_MIN and p != REF_PARTY]
    dummy_parties.sort(key=lambda p: -parties[p])

    def party_label(p):
        return p if (p in dummy_parties or p == REF_PARTY) else "其他小黨"
    has_other = any(party_label(r["party"]) == "其他小黨" for r in rows)

    cols = ["const"]
    cols += [f"黨={p}" for p in dummy_parties]
    if has_other:
        cols.append("黨=其他小黨")
    cols += [f"職={o}" for o in OFFICES if o != REF_OFFICE]
    cols += ["當選", "ln(得票數)"]

    X, y = [], []
    for r in rows:
        row = [1.0]
        for p in dummy_parties:
            row.append(1.0 if r["party"] == p else 0.0)
        if has_other:
            row.append(1.0 if party_label(r["party"]) == "其他小黨" else 0.0)
        for o in OFFICES:
            if o != REF_OFFICE:
                row.append(1.0 if r["office"] == o else 0.0)
        row.append(float(r["elected"]))
        row.append(math.log(r["votes"]))
        X.append(row)
        y.append(math.log(r["cpv"]))
    X = np.array(X)
    y = np.array(y)
    n, k = X.shape

    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    resid = y - X @ beta
    # HC1 穩健標準誤
    S = (X * (resid ** 2)[:, None]).T @ X
    cov = XtX_inv @ S @ XtX_inv * (n / (n - k))
    se = np.sqrt(np.diag(cov))
    z = beta / se
    p = [2 * (1 - 0.5 * (1 + math.erf(abs(zi) / math.sqrt(2)))) for zi in z]
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((resid ** 2).sum()) / ss_tot

    print("\n========== OLS：ln(每票成本) 迴歸（HC1 穩健標準誤）==========")
    print(f"參照組＝{REF_PARTY}・{REF_OFFICE}；n={n}，k={k}，R²={r2:.3f}")
    print(f"   係數為對數差；換算倍數＝exp(係數)，即相對參照組『每票成本』高/低多少 %\n")
    print(f"   {'變數':<14}{'係數':>9}{'穩健SE':>9}{'z':>7}{'p':>9}{'倍數':>8}  顯著")
    for name, b, s, zi, pi in zip(cols, beta, se, z, p):
        star = "***" if pi < 0.001 else "**" if pi < 0.01 else "*" if pi < 0.05 else ""
        mult = math.exp(b)
        pct = (mult - 1) * 100
        extra = f"  ×{mult:.2f}（{pct:+.0f}%）" if name.startswith("黨=") else ""
        print(f"   {name:<14}{b:>9.3f}{s:>9.3f}{zi:>7.1f}{pi:>9.4f}{mult:>8.2f}  {star}{extra}")
    print("\n   讀法：黨 dummy 係數>0＝控制職位/當選/選區規模後，該黨每票成本仍高於國民黨。")


def main():
    rows = load()
    print(f"樣本（有票數且有獻金、有黨籍）：{len(rows)} 位候選人")
    ranking(rows)
    ols(rows)


if __name__ == "__main__":
    main()
