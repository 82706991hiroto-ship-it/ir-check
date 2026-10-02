#!/usr/bin/env python3
"""EDINET の有価証券報告書から、チェックシートの自動採点に使う数字(fin/N.json)を作り直す。

毎月 GitHub Actions で動かし、直近に提出された有報の会社だけ、期が新しくなっていれば差し替える。

  EDINET_API_KEY=... python3 tools/edinet_fin.py update --days 45
  EDINET_API_KEY=... python3 tools/edinet_fin.py doc S100XXXX     # 1本だけ解析して表示(確認用)

数字は有報「主要な経営指標等の推移」の5期分(古い順)。金額は百万円、比率は小数。
出典: EDINET(公共データ利用規約 PDL1.0)。
"""
import argparse, csv, datetime as dt, io, json, os, re, sys, time, zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

API = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
RELS = ["Prior4Year", "Prior3Year", "Prior2Year", "Prior1Year", "CurrentYear"]  # 古い順
NICE = [1.1, 1.2, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 100]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 項目ごとの要素名。前にあるものほど優先(IFRS・米国基準の連結 → 日本基準)
SUMMARY = {
    "sales": ["RevenueIFRS", "RevenuesUSGAAP", "NetSales", "OperatingRevenue1", "OperatingRevenue2",
              "Revenue", "OrdinaryIncome", "GrossOperatingRevenue"],
    "eps": ["BasicEarningsLossPerShareIFRS", "BasicEarningsLossPerShareUSGAAP", "BasicEarningsLossPerShare"],
    "eq": ["RatioOfOwnersEquityToGrossAssetsIFRS", "EquityToAssetRatioIFRS", "EquityToAssetRatioUSGAAP", "EquityToAssetRatio"],
    "roe": ["RateOfReturnOnEquityIFRS", "RateOfReturnOnEquityUSGAAP", "RateOfReturnOnEquity"],
    "ocf": ["CashFlowsFromUsedInOperatingActivitiesIFRS", "CashFlowsFromUsedInOperatingActivitiesUSGAAP",
            "NetCashProvidedByUsedInOperatingActivities"],
    "cash": ["CashAndCashEquivalentsIFRS", "CashAndCashEquivalentsUSGAAP", "CashAndCashEquivalents"],
    "div": ["DividendPaidPerShare"],
    "interim": ["InterimDividendPaidPerShare"],
    "bps": ["EquityAttributableToOwnersOfParentPerShareIFRS", "EquityAttributableToOwnersOfParentPerShareUSGAAP",
            "NetAssetsPerShare"],
    "payout": ["PayoutRatio"],
    "shares": ["TotalNumberOfIssuedShares"],
    "opinc": ["OperatingProfitLossIFRS", "OperatingIncomeIFRS", "OperatingIncomeLossUSGAAP", "OperatingIncomeLoss"],
}
MONEY = {"sales", "ocf", "cash", "opinc"}
# 配当政策の文章から、配当を下げにくい方針(累進配当・DOE)を拾う
POLICY = {
    "累進配当": re.compile(r"累進(的な)?配当|減配(は|を)?(せず|しない|行わない|行わず)|配当(の)?(水準)?を?維持(または|もしくは|又は|ないし)増配|前期(実績)?を下限"),
    "DOE": re.compile(r"DOE|ＤＯＥ|(株主|自己|純)資本配当率"),
}
POLICY_NEG = re.compile(r"(累進配当|DOE|ＤＯＥ)[^。]{0,15}(は採用して|を採用して)(い|お)?(ません|ない|りません)")


def policy(text):
    """(方針のタグ, 根拠の文(2つまで))"""
    text = re.sub(r"\s+", "", text or "")
    tags, quotes = [], []
    for sent in re.split(r"(?<=。)", text):
        if POLICY_NEG.search(sent):
            continue
        for tag, p in POLICY.items():
            if p.search(sent):
                if tag not in tags:
                    tags.append(tag)
                # 長い文は、見つけた語の前後だけを切り出す
                m = p.search(sent)
                if len(sent) <= 140:
                    q = sent
                else:
                    a = max(0, m.start() - 50)
                    q = ("…" if a > 0 else "") + sent[a:a + 130] + ("…" if a + 130 < len(sent) else "")
                if q not in quotes:
                    quotes.append(q)
    return tags, quotes[:2]


# 損益計算書側の営業利益(主要な経営指標に営業利益が無い会社が多いため)
PL_OPINC = ["jpigp_cor:OperatingProfitLossIFRS", "jppfs_cor:OperatingIncome"]


def key():
    k = os.environ.get("EDINET_API_KEY")
    if not k:
        sys.exit("EDINET_API_KEY が設定されていません")
    return k


def get(url, params=None, tries=5, auth=True):
    params = dict(params or {})
    if auth:
        params["Subscription-Key"] = key()
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=90)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    return None


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def parse_csv(text):
    """有報のXBRL(CSV)から、5期分の系列と当期の営業利益を取り出す。"""
    facts = {}
    fyend = None
    for r in csv.reader(io.StringIO(text), delimiter="\t"):
        if len(r) < 9:
            continue
        el, ctx, v = r[0], r[2], r[8]
        if el == "jpdei_cor:CurrentFiscalYearEndDateDEI":
            fyend = v
        facts[(el, ctx)] = v

    by_local = {}
    for (el, ctx), v in facts.items():
        if el.endswith("SummaryOfBusinessResults"):
            by_local[(el.split(":")[-1][:-len("SummaryOfBusinessResults")], ctx)] = num(v)

    def series(local, kind, suffix):
        return [by_local.get((local, rel + kind + suffix)) for rel in RELS]

    def filled(s):
        return sum(v is not None for v in s)

    out = {"fyend": fyend}
    ptext = facts.get(("jpcrp_cor:DividendPolicyTextBlock", "FilingDateInstant"))
    out["policy"] = policy(ptext) if ptext else None
    for field, names in SUMMARY.items():
        kind = "Instant" if field in ("eq", "cash", "bps", "shares") else "Duration"
        best = None
        for name in names:
            cons, solo = series(name, kind, ""), series(name, kind, "_NonConsolidatedMember")
            # 連結を使う。連結の値が1期分以下(連結を始めたばかりなど)なら個別。
            # 1株配当・配当性向・株数は個別の表に載るので、こちらは個別を先に見る
            pref = (solo, cons) if field in ("div", "interim", "payout", "shares") else (cons, solo)
            pick = pref[0] if filled(pref[0]) >= 2 or not filled(pref[1]) else pref[1]
            if filled(pick):
                best = pick
                break
        out[field] = best
    if not out.get("opinc") or out["opinc"][-1] is None:
        for el in PL_OPINC:
            for ctx in ("CurrentYearDuration", "CurrentYearDuration_NonConsolidatedMember"):
                v = num(facts.get((el, ctx), ""))
                if v is not None:
                    out["opinc"] = [None, None, None, None, v]
                    break
            if out.get("opinc"):
                break
    return out


def snap(ratio):
    if ratio is None or ratio <= 0:
        return None
    if abs(ratio - 1) <= 0.05:
        return 1.0
    for n in NICE:
        if abs(ratio / n - 1) <= 0.06:
            return float(n)
    return None


def adjust_div(divs, shares, interims=None):
    """古い順の1株配当を、当期の株数基準に割り戻す(分割前の実額で載っている会社だけ)。

    期の途中で分割した年は、中間配当が分割前・期末配当が分割後の株数で払われ、年間の1株配当が混ざった値になる。
    中間配当が分かれば、中間配当を分割の倍率で割り戻して期末配当と足し、分割後の株数基準の年間配当に直す。
    """
    out = list(divs)
    interims = interims or [None] * len(divs)
    mixed = [False] * len(divs)
    for t in range(1, len(divs)):
        f = snap(shares[t] / shares[t - 1]) if shares[t] and shares[t - 1] else 1.0
        it = interims[t]
        if f and f >= 1.5 and it and 0 < it < divs[t] and divs[t - 1] > 0:
            # 中間配当が分割前の水準(前期の年間配当の半分前後)なら、その年は混ざっている。
            # 分割後の水準なら前期の半分÷倍率前後になるので、その間(0.5÷√倍率)で見分ける
            if it / divs[t - 1] > 0.5 / f ** 0.5:
                out[t] = round(it / f + (divs[t] - it), 2)
                mixed[t] = True
    divs = list(out)
    factor = 1.0
    for t in range(len(divs) - 2, -1, -1):
        f = snap(shares[t + 1] / shares[t]) if shares[t] and shares[t + 1] else 1.0
        # 株数が分割らしい倍率で増え、配当がほぼその分下がっていれば、分割前の実額とみなす
        # 混ざった年を直したところは分割が確かなので、必ず割り戻す
        if f and f > 1 and divs[t] > 0 and (mixed[t + 1] or divs[t + 1] / divs[t] < 0.75):
            factor *= f
        out[t] = round(divs[t] / factor, 2)
    return trim(out, shares)


def trim(out, shares):
    # 期の途中の分割で中間(分割前)と期末(分割後)が混ざった年は、40%を超えて下がって見える。
    # 株数が動いた期の近くでそうなっていたら、その年より古い期を外す。3倍を超える増配(上場前の年度の混入)も同様
    def moved(i):
        return 0 < i < len(shares) and shares[i] and shares[i - 1] and snap(shares[i] / shares[i - 1]) != 1.0
    for t in range(len(out) - 1, 0, -1):
        old, new = out[t - 1], out[t]
        if old > 0 and ((new / old < 0.6 and any(moved(i) for i in (t - 1, t, t + 1))) or new / old > 3):
            out = out[t:]
            break
    # 無配だった最初の期は外す(そこからの増配を減配と取り違えないため)
    while len(out) > 1 and out[0] <= 0:
        out.pop(0)
    return out


def tail(series):
    """古い順の系列から、値の無い期を除く。"""
    if not series:
        return None
    out = [v for v in series if v is not None]
    return out or None


def record(parsed, name, sector):
    """fin/N.json の1社分。キーはチェックシートの scoreRecord が読むもの。"""
    rec = {"n": name, "sec": sector, "fy": parsed["fyend"]}
    for field in ("sales", "eps", "ocf", "cash"):
        s = tail(parsed.get(field))
        if s and len(s) >= 2:
            rec[field] = [round(v / 1e6, 0) if field in MONEY else v for v in s]
    for field in ("eq", "roe"):
        s = tail(parsed.get(field))
        # 比率の欄に別の数字を入れている会社がある(自己資本比率に1株純資産など)
        if s and all(-5 < v < 5 for v in s):
            rec[field] = [round(v, 4) for v in s]
    d, sh = parsed.get("div"), parsed.get("shares") or [None] * 5
    it = parsed.get("interim") or [None] * 5
    if d and d[-1] is not None:
        keep = [i for i, v in enumerate(d) if v is not None]
        rec["div"] = adjust_div([d[i] for i in keep], [sh[i] for i in keep], [it[i] for i in keep])
    b = tail(parsed.get("bps"))
    if b:
        rec["bps"] = b[-1]
    o, s = parsed.get("opinc"), parsed.get("sales")
    if o and o[-1] is not None and s and s[-1]:
        rec["opm"] = round(o[-1] / s[-1], 4)
    p = parsed.get("payout")
    if p and p[-1] is not None:
        rec["payout"] = round(p[-1], 4)
    if parsed.get("policy") is not None:
        rec["pc"] = 1  # 配当政策を読んだ
        tags, quotes = parsed["policy"]
        if tags:
            rec["pol"], rec["polq"] = tags, quotes
    return rec


def download(doc_id):
    r = get(API + "/documents/" + doc_id, {"type": 5})
    if r is None or r.content[:2] != b"PK":
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        name = next(n for n in z.namelist() if re.search(r"XBRL_TO_CSV/jpcrp.*asr.*\.csv$", n))
        return parse_csv(z.read(name).decode("utf-16"))
    except (StopIteration, zipfile.BadZipFile, UnicodeDecodeError):
        return None


def recent_filings(days):
    """直近 days 日に提出された有価証券報告書(証券コードのあるもの)。同じ会社は新しい期の1本だけ。"""
    today = dt.date.today()
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(days)]
    dates = [d for d in dates if dt.date.fromisoformat(d).weekday() < 5]

    def one(d):
        r = get(API + "/documents.json", {"date": d, "type": 2})
        return (r.json().get("results") or []) if r is not None else []

    by = {}
    with ThreadPoolExecutor(4) as ex:
        for rows in ex.map(one, dates):
            for x in rows:
                if (x.get("docTypeCode") == "120" and x.get("ordinanceCode") == "010" and x.get("secCode")
                        and x.get("csvFlag") == "1" and x.get("withdrawalStatus") == "0" and x.get("periodEnd")):
                    code = x["secCode"][:4].upper()
                    if code not in by or (x["periodEnd"], x["submitDateTime"]) > (by[code]["periodEnd"], by[code]["submitDateTime"]):
                        by[code] = x
    return by


def code_list():
    """EDINETコードリストから {証券コード4桁: (提出者名, 業種)}。取れなければ空。"""
    r = get(CODELIST, auth=False, tries=3)
    if r is None or r.content[:2] != b"PK":
        print("EDINETコードリストを取得できませんでした。新規の会社は社名・業種が分からないので来月に回します")
        return {}
    z = zipfile.ZipFile(io.BytesIO(r.content))
    text = z.read(next(n for n in z.namelist() if n.lower().endswith(".csv"))).decode("cp932")
    rows = list(csv.reader(io.StringIO(text)))
    head = next(i for i, r in enumerate(rows) if "証券コード" in r)
    h = rows[head]
    ic, iname, isec = h.index("証券コード"), h.index("提出者名"), h.index("提出者業種")
    out = {}
    for r in rows[head + 1:]:
        if len(r) > max(ic, iname, isec) and r[ic].strip():
            out[r[ic].strip()[:4].upper()] = (r[iname].strip(), r[isec].strip())
    return out


def short_name(n):
    return re.sub(r"^(株式会社|\(株\))|(株式会社|\(株\))$", "", n.strip()).strip()


def load_fin():
    fin = {}
    for fn in sorted(os.listdir(os.path.join(ROOT, "fin"))):
        if fn.endswith(".json"):
            with open(os.path.join(ROOT, "fin", fn), encoding="utf-8") as f:
                fin[fn[:-5]] = json.load(f)
    return fin


def save_fin(fin):
    for head, m in fin.items():
        with open(os.path.join(ROOT, "fin", head + ".json"), "w", encoding="utf-8") as f:
            json.dump(dict(sorted(m.items())), f, ensure_ascii=False, separators=(",", ":"))


def write_policy(fin):
    """累進配当・DOEの会社の一覧(policy.json)。一覧ページが読む小さなデータ"""
    rows = []
    for m in fin.values():
        for code, r in m.items():
            if not r.get("pol"):
                continue
            d, e = r.get("div") or [], r.get("eps") or []
            up = 0
            for k in range(len(d) - 1, 0, -1):
                if d[k] > d[k - 1]:
                    up += 1
                else:
                    break
            rows.append({
                "c": code, "n": r.get("n", ""), "s": r.get("sec", ""), "p": r["pol"], "fy": (r.get("fy") or "")[:7],
                "d": d, "po": round(d[-1] / e[-1] * 100, 1) if d and e and e[-1] > 0 else None,
                "up": up, "q": r.get("polq", []),
            })
    rows.sort(key=lambda x: x["c"])
    out = {"built": dt.date.today().isoformat(), "checked": sum(1 for m in fin.values() for r in m.values() if r.get("pc")),
           "rows": rows}
    with open(os.path.join(ROOT, "policy.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return len(rows)


def cmd_update(days):
    fin = load_fin()
    filings = recent_filings(days)
    todo = []
    for code, x in filings.items():
        old = fin.get(code[0], {}).get(code)
        if old and old.get("fy", "") >= x["periodEnd"]:
            continue
        todo.append((code, x))
    print("有報", len(filings), "件のうち、期が新しい会社", len(todo), "件", flush=True)
    todo = [(c, x) for c, x in todo if re.match(r"^[1-9][0-9A-Z]{3}$", c)]
    names = code_list() if any(not fin.get(c[0], {}).get(c) for c, _ in todo) else {}
    if not names:
        todo = [(c, x) for c, x in todo if fin.get(c[0], {}).get(c)]
    changed = []

    def one(item):
        code, x = item
        return code, x, download(x["docID"])

    with ThreadPoolExecutor(4) as ex:
        for code, x, parsed in ex.map(one, todo):
            if not parsed or not parsed.get("fyend"):
                print("  解析できず", code, x["docID"])
                continue
            old = fin.setdefault(code[0], {}).get(code)
            if old:
                name, sector = old.get("n"), old.get("sec", "")
            else:
                if code not in names:
                    continue
                name, sector = names[code]
                name = short_name(name)
            fin[code[0]][code] = record(parsed, name, sector)
            changed.append(code + ("" if old else "(新規)"))
    save_fin(fin)
    print("累進配当・DOEの会社", write_policy(fin), "社")
    print("更新", len(changed), "社:", " ".join(changed[:60]) + (" …" if len(changed) > 60 else ""))
    return changed


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("policy")
    u = sub.add_parser("update")
    u.add_argument("--days", type=int, default=45)
    d = sub.add_parser("doc")
    d.add_argument("doc_id")
    a = ap.parse_args()
    if a.cmd == "update":
        cmd_update(a.days)
    elif a.cmd == "policy":
        print(write_policy(load_fin()))
    else:
        print(json.dumps(record(download(a.doc_id), "", ""), ensure_ascii=False))


if __name__ == "__main__":
    main()
