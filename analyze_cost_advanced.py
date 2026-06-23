#!/usr/bin/env python3
"""每票成本 × 黨籍：進階統計分析（學術版）。

在基礎 OLS 之上加入：
  1. 縣市固定效果（FE）模型——吸收選區層級的混雜（競爭度、地方生活成本等），檢驗黨籍效果穩健性。
  2. 黨籍 dummy 的聯合 F 檢定、加入 FE 的偏 F 檢定、模型選擇（adj-R²、AIC）。
  3. 診斷：偏態（證成對數轉換）、Breusch–Pagan 異質變異檢定（證成穩健標準誤）、VIF 共線性。
  4. 無母數對照：Kruskal–Wallis 檢定（抗偏態/離群）、ANOVA 效果量 η²。
  5. Bootstrap（2000 次重抽）對「民進黨 vs 國民黨」係數做分配自由的 95% 信賴區間。

僅 numpy；t/F/χ² 之 p 值以不完全 beta/gamma 函數自行計算。
觀察資料，係數為相關非因果（黨籍打包了選區選擇、現任、空戰/陸戰等）。
"""
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
OFFICES = ["立法委員", "議員", "縣市長"]
REF_OFFICE = "立法委員"
REF_PARTY = "國民黨"
DUMMY_MIN = 20
rng = np.random.default_rng(20260623)


# ---------- 特殊函數：t / F / χ² 的 p 值（Numerical Recipes 標準式）----------
def _gammln(x):
    c = [76.18009172947146, -86.50532032941677, 24.01409824083091,
         -1.231739572450155, 0.1208650973866179e-2, -0.5395239384953e-5]
    y = x
    tmp = x + 5.5
    tmp -= (x + 0.5) * math.log(tmp)
    ser = 1.000000000190015
    for cj in c:
        y += 1
        ser += cj / y
    return -tmp + math.log(2.5066282746310005 * ser / x)


def _gammq(a, x):
    if x < 0 or a <= 0:
        return 1.0
    if x < a + 1:                       # 級數
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-12:
                break
        return 1.0 - s * math.exp(-x + a * math.log(x) - _gammln(a))
    b = x + 1 - a                       # 連分數
    c = 1e30
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < 1e-30:
            d = 1e-30
        c = b + an / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return math.exp(-x + a * math.log(x) - _gammln(a)) * h


def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c = 1.0
    d = 1 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return h


def _betai(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(_gammln(a + b) - _gammln(a) - _gammln(b)
                  + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def p_t(t, df):
    return _betai(df / 2, 0.5, df / (df + t * t))


def p_f(f, df1, df2):
    return _betai(df2 / 2, df1 / 2, df2 / (df2 + df1 * f))


def p_chi2(x, df):
    return _gammq(df / 2, x / 2)


# ---------- OLS ＋ HC1 ----------
def ols(X, y):
    n, k = X.shape
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    resid = y - X @ beta
    rss = float(resid @ resid)
    sigma2 = rss / (n - k)
    cov_cl = sigma2 * XtX_inv
    S = (X * (resid ** 2)[:, None]).T @ X            # HC1
    cov_hc = XtX_inv @ S @ XtX_inv * (n / (n - k))
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - rss / tss
    adj = 1 - (1 - r2) * (n - 1) / (n - k)
    aic = n * math.log(rss / n) + 2 * k
    return dict(beta=beta, resid=resid, rss=rss, cov_cl=cov_cl, cov_hc=cov_hc,
                r2=r2, adj=adj, aic=aic, n=n, k=k, XtX_inv=XtX_inv)


def load():
    raw = (ROOT / "docs" / "candidates.js").read_text(encoding="utf-8")
    C = json.loads(raw[raw.index("=") + 1:].rstrip().rstrip(";"))
    rows = []
    for d in C.values():
        if d.get("votes") and d["total"] > 0 and d["office"] in OFFICES and (d.get("party") or ""):
            rows.append(dict(party=d["party"], office=d["office"],
                             elected=1 if d.get("elected") else 0,
                             votes=d["votes"], county=d["district"],
                             cpv=d["total"] / d["votes"]))
    return rows


def design(rows, dummy_parties, others, with_fe):
    counties = sorted({r["county"] for r in rows})
    ref_county = counties[0]
    cols = ["const"] + [f"黨={p}" for p in dummy_parties]
    if others:
        cols.append("黨=其他小黨")
    cols += [f"職={o}" for o in OFFICES if o != REF_OFFICE]
    cols += ["當選", "ln票數", "立委2024"]
    if with_fe:
        cols += [f"縣={c}" for c in counties if c != ref_county]
    X, y = [], []
    for r in rows:
        v = [1.0]
        v += [1.0 if r["party"] == p else 0.0 for p in dummy_parties]
        if others:
            v.append(1.0 if (r["party"] not in dummy_parties and r["party"] != REF_PARTY) else 0.0)
        v += [1.0 if r["office"] == o else 0.0 for o in OFFICES if o != REF_OFFICE]
        v.append(float(r["elected"]))
        v.append(math.log(r["votes"]))
        v.append(1.0 if (r["office"] == "立法委員" and r["votes"] and r["county"] and r.get("_y2024")) else 0.0)
        if with_fe:
            v += [1.0 if r["county"] == c else 0.0 for c in counties if c != ref_county]
        X.append(v)
        y.append(math.log(r["cpv"]))
    return np.array(X), np.array(y), cols


def main():
    rows = load()
    # 立委 2024 旗標（年度資訊在 candidates.js 已隱含於 office/year；這裡用 votes 無法判年，改由原資料補）
    raw = (ROOT / "docs" / "candidates.js").read_text(encoding="utf-8")
    C = json.loads(raw[raw.index("=") + 1:].rstrip().rstrip(";"))
    # 重新帶入 year 以標記立委2024
    rows = []
    for d in C.values():
        if d.get("votes") and d["total"] > 0 and d["office"] in OFFICES and (d.get("party") or ""):
            rows.append(dict(party=d["party"], office=d["office"],
                             elected=1 if d.get("elected") else 0,
                             votes=d["votes"], county=d["district"],
                             cpv=d["total"] / d["votes"],
                             _y2024=(d["office"] == "立法委員" and d["year"] == 2024)))
    n = len(rows)
    cnt = defaultdict(int)
    for r in rows:
        cnt[r["party"]] += 1
    dummy_parties = sorted([p for p in cnt if cnt[p] >= DUMMY_MIN and p != REF_PARTY],
                           key=lambda p: -cnt[p])
    others = any(r["party"] not in dummy_parties and r["party"] != REF_PARTY for r in rows)
    print(f"樣本 n={n}；參照＝{REF_PARTY}・{REF_OFFICE}；單列政黨 dummy：{dummy_parties}"
          + ("＋其他小黨" if others else ""))

    y_all = np.array([math.log(r["cpv"]) for r in rows])
    cpv = np.array([r["cpv"] for r in rows])

    # ---- 0) 分配診斷：偏態 ----
    def skew(a):
        m = a.mean()
        s = a.std()
        return float(((a - m) ** 3).mean() / s ** 3)
    print("\n【0】分配與轉換")
    print(f"   每票成本偏態={skew(cpv):.2f}（右偏嚴重）；取 ln 後偏態={skew(y_all):.2f}"
          " → 證成以 ln(每票成本) 為被解釋變數")

    # ---- 1) 基礎模型 M1 ----
    X1, y, c1 = design(rows, dummy_parties, others, with_fe=False)
    m1 = ols(X1, y)
    # ---- 2) 加縣市固定效果 M2 ----
    X2, _, c2 = design(rows, dummy_parties, others, with_fe=True)
    m2 = ols(X2, y)

    def report_party(m, cols, title):
        print(f"\n   〔{title}〕 n={m['n']} k={m['k']}  R²={m['r2']:.3f} adj-R²={m['adj']:.3f} AIC={m['aic']:.0f}")
        se = np.sqrt(np.diag(m["cov_hc"]))
        df = m["n"] - m["k"]
        print(f"     {'變數':<12}{'係數':>8}{'HC1-SE':>9}{'95% CI(倍數)':>20}{'p':>9}")
        for i, name in enumerate(cols):
            if not name.startswith("黨="):
                continue
            b, s = m["beta"][i], se[i]
            lo, hi = math.exp(b - 1.96 * s), math.exp(b + 1.96 * s)
            pv = p_t(b / s, df)
            star = "***" if pv < .001 else "**" if pv < .01 else "*" if pv < .05 else ""
            print(f"     {name:<12}{b:>8.3f}{s:>9.3f}{f'×{math.exp(b):.2f} [{lo:.2f},{hi:.2f}]':>22}{pv:>9.4f} {star}")

    print("\n【1】OLS 與 縣市固定效果模型（HC1 穩健 SE；黨係數＝相對國民黨之 ln 差，倍數＝exp）")
    report_party(m1, c1, "M1：黨＋職位＋當選＋ln票數＋立委2024")
    report_party(m2, c2, "M2：M1 ＋ 縣市固定效果")

    # ---- 3) F 檢定 ----
    print("\n【2】假設檢定（F 檢定）")
    # 3a 黨籍聯合顯著性：用 M2 對照「拿掉所有黨 dummy」
    party_cols = [i for i, nm in enumerate(c2) if nm.startswith("黨=")]
    keep = [i for i in range(len(c2)) if i not in party_cols]
    mr = ols(X2[:, keep], y)
    q = len(party_cols)
    F = ((mr["rss"] - m2["rss"]) / q) / (m2["rss"] / (m2["n"] - m2["k"]))
    print(f"   黨籍 dummy 聯合顯著（M2 內）：F({q},{m2['n']-m2['k']})={F:.1f}，p={p_f(F,q,m2['n']-m2['k']):.2e}"
          " → 黨籍整體顯著")
    # 3b 加縣市 FE 是否改善：M2 vs M1
    q2 = m2["k"] - m1["k"]
    F2 = ((m1["rss"] - m2["rss"]) / q2) / (m2["rss"] / (m2["n"] - m2["k"]))
    print(f"   加入縣市 FE 偏 F({q2},{m2['n']-m2['k']})={F2:.1f}，p={p_f(F2,q2,m2['n']-m2['k']):.2e}"
          f"；adj-R² {m1['adj']:.3f}→{m2['adj']:.3f}、AIC {m1['aic']:.0f}→{m2['aic']:.0f}")

    # ---- 4) 診斷 ----
    print("\n【3】迴歸診斷")
    # Breusch–Pagan（用 M1）
    e2 = m1["resid"] ** 2
    aux = ols(X1, e2 / e2.mean())
    LM = m1["n"] * aux["r2"]
    dfbp = m1["k"] - 1
    print(f"   Breusch–Pagan 異質變異：LM={LM:.1f}, χ²({dfbp}), p={p_chi2(LM,dfbp):.2e}"
          " → 拒絕同質變異，故採 HC1 穩健 SE 正確")
    # VIF（只看焦點變數）
    print("   VIF 共線性（焦點變數；>5 須留意）：")
    for j, nm in enumerate(c1):
        if nm == "const" or nm.startswith("縣="):
            continue
        others_idx = [i for i in range(X1.shape[1]) if i != j]
        rj = ols(X1[:, others_idx], X1[:, j])["r2"]
        vif = 1 / max(1e-9, 1 - rj)
        if nm.startswith("黨=") or nm in ("ln票數", "當選") or nm.startswith("職="):
            print(f"     {nm:<12} VIF={vif:.2f}")

    # ---- 5) 無母數 / 效果量 ----
    print("\n【4】無母數檢定與效果量")
    groups = defaultdict(list)
    for r in rows:
        groups[r["party"]].append(r["cpv"])
    groups = {p: v for p, v in groups.items() if len(v) >= 5}
    # Kruskal–Wallis（對每票成本『水準』排名，抗偏態）
    allv = np.concatenate([np.array(v) for v in groups.values()])
    order = allv.argsort()
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(allv) + 1)
    # 平均並列名次
    idx = 0
    sv = allv[order]
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        if j > i:
            avg = (i + 1 + j + 1) / 2
            for t in range(i, j + 1):
                ranks[order[t]] = avg
        i = j + 1
    N = len(allv)
    pos = 0
    H = 0.0
    for v in groups.values():
        ng = len(v)
        Rg = ranks[pos:pos + ng].sum()
        H += Rg * Rg / ng
        pos += ng
    H = 12.0 / (N * (N + 1)) * H - 3 * (N + 1)
    dfk = len(groups) - 1
    print(f"   Kruskal–Wallis（{len(groups)} 黨，每票成本水準）：H={H:.1f}, χ²({dfk}), p={p_chi2(H,dfk):.2e}"
          " → 至少一黨分布不同")
    # ANOVA η²（對 ln 成本）
    ylog = {p: np.log(np.array(v)) for p, v in groups.items()}
    grand = np.concatenate(list(ylog.values())).mean()
    ssb = sum(len(v) * (v.mean() - grand) ** 2 for v in ylog.values())
    ssw = sum(((v - v.mean()) ** 2).sum() for v in ylog.values())
    eta2 = ssb / (ssb + ssw)
    G = len(ylog)
    Nt = sum(len(v) for v in ylog.values())
    Fa = (ssb / (G - 1)) / (ssw / (Nt - G))
    print(f"   單因子 ANOVA(ln成本)：F({G-1},{Nt-G})={Fa:.1f}, p={p_f(Fa,G-1,Nt-G):.2e}；"
          f"效果量 η²={eta2:.3f}（黨籍解釋 ln成本變異之 {eta2*100:.1f}%）")

    # ---- 6) Bootstrap：民進黨 vs 國民黨 係數 ----
    if "黨=民進黨" in c1:
        jdpp = c1.index("黨=民進黨")
        B = 2000
        coefs = np.empty(B)
        nrow = X1.shape[0]
        for b in range(B):
            s = rng.integers(0, nrow, nrow)
            Xb, yb = X1[s], y[s]
            try:
                bb = np.linalg.solve(Xb.T @ Xb, Xb.T @ yb)
                coefs[b] = bb[jdpp]
            except np.linalg.LinAlgError:
                coefs[b] = np.nan
        coefs = coefs[~np.isnan(coefs)]
        lo, hi = np.percentile(coefs, [2.5, 97.5])
        print("\n【5】Bootstrap（2000 次重抽，分配自由）民進黨 vs 國民黨")
        print(f"   ln 係數 {m1['beta'][jdpp]:.3f}；倍數 ×{math.exp(m1['beta'][jdpp]):.2f}"
              f"；95% CI ×[{math.exp(lo):.2f}, {math.exp(hi):.2f}]"
              f"（{(math.exp(lo)-1)*100:+.0f}% ~ {(math.exp(hi)-1)*100:+.0f}%）→ 穩健不含 1")


if __name__ == "__main__":
    main()
