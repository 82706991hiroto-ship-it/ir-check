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
# 「累進配当の導入を検討」はまだ宣言ではない(予備軍のいちばん強い手がかりにする)
POLICY_CONSIDER = re.compile(r"検討(を開始|を始め|して|中|いた|する|を進め)")
POLICY_NEG = re.compile(r"(累進配当|DOE|ＤＯＥ)[^。]{0,15}(は採用して|を採用して)(い|お)?(ません|ない|りません)")


def policy(text):
    """(方針のタグ, 根拠の文(2つまで))"""
    text = re.sub(r"\s+", "", text or "")
    tags, quotes = [], []
    for sent in re.split(r"(?<=。)", text):
        if POLICY_NEG.search(sent) or POLICY_CONSIDER.search(sent):
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


# まだ累進配当・DOEを宣言していないが、配当政策の書き方がそれに近い会社(予備軍)の手がかり
CAND_TEXT = {
    "導入を検討": re.compile(r"(累進(的な)?配当|DOE|ＤＯＥ|(株主|自己|純)資本配当率)[^。]{0,30}検討(を開始|を始め|して|中|いた|する|を進め)"),
    # 1株配当の額に下限(配当性向の下限は利益しだいなので含めない)
    "下限を設定": re.compile(r"\d+(\.\d+)?円[^。%％]{0,10}(を下限|を最低|を下回らない)|下限配当|最低配当|配当(金)?の下限(額)?を[^。%％]{0,6}円"),
    "増配を目指す": re.compile(r"(継続的|持続的|安定的|段階的|長期的)(な|に)増配|増配(を|の)(基本|継続|実現|目指|目標)"),
    # 方針の変更は、還元を強める向きのものだけ(配当性向の基準の付け替えなどは除く)
    "還元強化へ方針変更": re.compile(r"(配当|株主還元)(の)?方針[^。]{0,15}(見直|変更)(?=[^。]*(強化|充実|拡充|拡大|増配|引き上げ|引上げ|DOE|ＤＯＥ|累進|下限))|(強化|充実|拡充|拡大|増配|引き上げ|引上げ)[^。]{0,60}(配当|株主還元)(の)?方針[^。]{0,15}(見直|変更)"),
}


def policy_words(text):
    """配当政策に「中期経営計画」「自己株式」が出てくるか(予備軍の判定に使う)"""
    text = re.sub(r"\s+", "", text or "")
    return [w for w, pat in (("中計", r"中期経営計画"), ("自己株", r"自己株式")) if re.search(pat, text)]


def candidate_text(text):
    """予備軍の手がかり [(種類, 根拠の文)]。種類ごとに1つ"""
    text = re.sub(r"\s+", "", text or "")
    out = {}
    for sent in re.split(r"(?<=。)", text):
        for tag, p in CAND_TEXT.items():
            m = p.search(sent)
            if m and tag not in out:
                a = max(0, m.start() - 50)
                out[tag] = sent if len(sent) <= 140 else ("…" if a > 0 else "") + sent[a:a + 130] + ("…" if a + 130 < len(sent) else "")
    return list(out.items())


# 「配当性向○%を目安(目標・程度・以上)」のように、利益に連動して配当を決める方針。
# 利益が落ちると配当も下がりやすいので、高い配当性向の会社の説明に使う
PAYOUT_TARGET = re.compile(r"配当性向[^。]{0,25}?(\d{2,3}(?:\.\d)?)\s*[%％](?:程度|前後|を目安|を目途|を目処|目途|目処|以上|を目標|目標|を基準|水準|を基本|を下限)")


def payout_target(text):
    """(配当性向の目安 %, 根拠の文) か None"""
    text = re.sub(r"\s+", "", text or "")
    for sent in re.split(r"(?<=。)", text):
        m = PAYOUT_TARGET.search(sent)
        if m and 10 <= float(m.group(1)) <= 150:
            a = max(0, m.start() - 40)
            q = sent if len(sent) <= 140 else ("…" if a > 0 else "") + sent[a:a + 130] + "…"
            return float(m.group(1)), q
    return None


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


class SplicedList(list):
    """前期までの連結と当期の単体をつないだ系列の目印。"""


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
    # 「配当の状況」(剰余金の配当)の表: 当期の1株配当が「－」の年を補うのに使う
    rows = {}
    for (el, ctx), v in facts.items():
        if el in ("jpcrp_cor:DividendPerShareDividendsOfSurplus", "jpcrp_cor:TotalAmountOfDividendsDividendsOfSurplus"):
            mm = re.search(r"Row(\d+)Member", ctx)
            if mm and num(v) is not None:
                rows.setdefault(int(mm.group(1)), {})["dps" if "PerShare" in el else "amt"] = num(v)
    out["surplus"] = [(x["dps"], x.get("amt")) for _, x in sorted(rows.items()) if x.get("dps")]
    ptext = facts.get(("jpcrp_cor:DividendPolicyTextBlock", "FilingDateInstant"))
    out["policy"] = policy(ptext) if ptext else None
    out["potarget"] = payout_target(ptext) if ptext else None
    out["candtext"] = candidate_text(ptext) if ptext else []
    out["kw"] = policy_words(ptext) if ptext else []
    for field, names in SUMMARY.items():
        kind = "Instant" if field in ("eq", "cash", "bps", "shares") else "Duration"
        best, best_rank = None, None
        for name in names:
            cons, solo = series(name, kind, ""), series(name, kind, "_NonConsolidatedMember")
            # 連結を使う。連結の値が1期分以下(連結を始めたばかりなど)なら個別。
            # 1株配当・配当性向・株数は個別の表に載るので、こちらは個別を先に見る
            pref = (solo, cons) if field in ("div", "interim", "payout", "shares") else (cons, solo)
            pick = pref[0] if filled(pref[0]) >= 2 or not filled(pref[1]) else pref[1]
            # 連結を始めたばかりで連結が当期1期分だけでも、当期の判定は連結で行う(単体の1株利益で配当性向がずれないように)
            if field not in ("div", "interim", "payout", "shares") and cons[-1] is not None:
                pick = cons
            # 連結をやめた会社など、片方にしか当期の値が無いときは当期のある方を使う
            # (例: JTP。連結の古い2期だけが残り、当期は単体にしかない。古い連結の値を「当期」にしない)
            if pick[-1] is None:
                other = solo if pick is cons else cons
                if other[-1] is not None:
                    # 当期に連結をやめた会社(例: 前澤給装工業)は、前期までが連結、当期が単体にしか無い。
                    # 単体は当期1期だけなので、前期までの連結とつないで推移を残す(つないだことは sw に書く)
                    if pick is cons and filled(other) == 1 and filled(pick) >= 1:
                        pick = SplicedList(pick[:-1] + [other[-1]])
                    else:
                        pick = other
            # IFRSなどへの移行の前後は、参考の2期分だけ別の基準が載ることがある(例: 東京海上)。
            # 当期の値があり、より多くの期が載っている基準を正式なものとして使う
            # 連結(1株配当などは個別)の当期の値を、別の側(例: 持株会社の単体)の当期より優先する(例: リガクHD)
            rank = (pick[-1] is not None, pick is pref[0], filled(pick))
            if filled(pick) and (best is None or rank > best_rank):
                best, best_rank = pick, rank
        out[field] = best
        if isinstance(best, SplicedList):
            out.setdefault("sw", []).append(field)
    # 1株純資産は連結を優先する。IFRSの会社には、連結の1株純資産を「自己資本比率」の項目名で載せているところがある
    # (例: 伊藤忠商事。値が5を超えるので比率ではないと分かる)。連結が見つからなければ、単体は使わず入れない
    cons_bps = None
    for name in ("EquityAttributableToOwnersOfParentPerShareIFRS", "EquityAttributableToOwnersOfParentPerShareUSGAAP", "NetAssetsPerShare"):
        sr = series(name, "Instant", "")
        if sr[-1] is not None:
            cons_bps = sr
            break
    if cons_bps is None:
        sr = series("EquityToAssetRatioIFRS", "Instant", "")
        if sr[-1] is not None and sr[-1] > 5:
            cons_bps = sr
    eps_cons = any(series(n, "Duration", "")[-1] is not None for n in SUMMARY["eps"])
    if cons_bps is not None:
        out["bps"] = cons_bps
    elif eps_cons:
        out["bps"] = None
    # 株価収益率(PER)。有報の期末時点の株価が分かるので、PBR(= PER × 1株利益 ÷ 1株純資産)を出せる
    for name in ("PriceEarningsRatioIFRS", "PriceEarningsRatioUSGAAP", "PriceEarningsRatio"):
        v = by_local.get((name, "CurrentYearDuration"))
        if v is not None:
            out["per"] = v
            break
    # 純資産(会社の大きさ。予備軍の判定に使う)。連結、無ければ単体
    for suf in ("", "_NonConsolidatedMember"):
        v = by_local.get(("NetAssets", "CurrentYearInstant" + suf))
        if v is not None:
            out["net"] = v
            break
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
            # 1.1〜1.25倍の小さな分割は、株数がほぼぴったり(1%以内)のときだけ(増資・合併と取り違えない)
            if n < 1.3 and abs(ratio / n - 1) > 0.01:
                return 1.0
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
        # 混ざった年は、期末分が分割後の小さな額なので、年間の合計が前期より下がって見える。
        # 下がっていなければ、有報が前の期をもう分割後の額に直して載せている(例: 三井住友FG)ので割り戻さない
        if f and f >= 1.5 and it and 0 < it < divs[t] and divs[t - 1] > 0 and divs[t] / divs[t - 1] < 0.85:
            # 中間配当が分割前の水準(前期の年間配当の半分前後)なら、その年は混ざっている。
            # 分割後の水準なら前期の半分÷倍率前後になるので、その間(0.5÷√倍率)で見分ける
            if it / divs[t - 1] > 0.5 / f ** 0.5:
                out[t] = round(it / f + (divs[t] - it), 2)
                mixed[t] = True
    divs = list(out)
    # 期末日に分割の効力が出る会社は、その期の株数はもう分割後なのに配当は分割前の実額で載る
    # (翌期に配当が「下がって」見える)。株数の跳ねを翌期の境目に移して、その期まで割り戻す(HAITOと同じ)
    shares = list(shares)
    for t in range(1, len(divs) - 1):
        f = snap(shares[t] / shares[t - 1]) if shares[t] and shares[t - 1] else 1.0
        if (f and f >= 1.5 and not mixed[t] and divs[t - 1] and divs[t] and divs[t + 1]
                and divs[t] / divs[t - 1] >= 0.8 and divs[t + 1] / divs[t] < 0.8
                and 0.95 <= divs[t + 1] / (divs[t] / f) <= 2.6):
            shares[t] = shares[t - 1]
    factor = 1.0
    for t in range(len(divs) - 2, -1, -1):
        f = snap(shares[t + 1] / shares[t]) if shares[t] and shares[t + 1] else 1.0
        # 株数が分割らしい倍率で増え、配当がほぼその分下がっていれば、分割前の実額とみなす
        # (2倍の分割で20円→15円のように、増配込みで下がり方が浅い年もある)
        # 混ざった年を直したところは分割が確かなので、必ず割り戻す
        if f and f > 1 and divs[t] > 0 and (mixed[t + 1] or divs[t + 1] / divs[t] < min(0.85, 1.6 / f)):
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
    # 当期の欄が空(「－」)の項目は、古い期の値が「当期」として使われないように、その項目ごと入れない
    for field in ("sales", "ocf", "cash", "eq", "eps", "bps"):
        raw_s = parsed.get(field)
        if raw_s and raw_s[-1] is None:
            parsed[field] = None
    # ROE は赤字の期に「－」になる。当期が空なら 1株利益 ÷ 1株純資産 で当期を出す(無ければ入れない)
    roe_s, eps_s, bps_s = parsed.get("roe"), parsed.get("eps"), parsed.get("bps")
    if roe_s and roe_s[-1] is None:
        if eps_s and eps_s[-1] is not None and bps_s and bps_s[-1]:
            parsed["roe"] = [None] * (len(roe_s) - 1) + [round(eps_s[-1] / bps_s[-1], 4)]
        else:
            parsed["roe"] = None
    for field in ("sales", "eps", "ocf", "cash"):
        s = tail(parsed.get(field))
        # 1株利益は当期1期分だけでも残す(配当性向に使う)。売上・CF・現金は推移を見るので2期以上
        if s and (len(s) >= 2 or field == "eps"):
            rec[field] = [round(v / 1e6, 0) if field in MONEY else v for v in s]
    for field in ("eq", "roe"):
        s = tail(parsed.get(field))
        # 比率の欄に別の数字を入れている会社がある(自己資本比率に1株純資産など)
        if s and all(-5 < v < 5 for v in s):
            rec[field] = [round(v, 4) for v in s]
    sw = [f for f in (parsed.get("sw") or []) if f in rec or f in ("eq", "roe")]
    if sw:
        rec["sw"] = sw
    d, sh = parsed.get("div"), parsed.get("shares") or [None] * 5
    it = parsed.get("interim") or [None] * 5
    if d and d[-1] is None and any(v is not None for v in d[:-1]):
        # 当期が「－」: 期の途中で分割した年は「配当の状況」から期末の株数基準で計算、行が無く中間も無ければ無配
        rows = parsed.get("surplus") or []
        if not rows and not it[-1]:
            d = d[:-1] + [0.0]
        elif rows and all(a for _, a in rows):
            last = rows[-1][1] / rows[-1][0]
            d = d[:-1] + [round(sum(x / (snap(last / (a / x)) or 1.0) for x, a in rows), 2)]
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
    if parsed.get("potarget"):
        rec["pot"], rec["potq"] = parsed["potarget"]
    if parsed.get("candtext") and not rec.get("pol"):
        rec["polc"] = parsed["candtext"]
    if parsed.get("kw"):
        rec["kw"] = parsed["kw"]
    if parsed.get("net"):
        rec["net"] = round(parsed["net"] / 1e8)  # 億円
    if parsed.get("per") and parsed["per"] > 0:
        rec["per"] = round(parsed["per"], 2)
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


def code_list(listed_only=False):
    """EDINETコードリストから {証券コード4桁: (提出者名, 業種)}。取れなければ空。
    listed_only なら上場区分が「上場」の会社だけ(上場をやめた会社を足さないため)。"""
    r = get(CODELIST, auth=False, tries=3)
    if r is None or r.content[:2] != b"PK":
        print("EDINETコードリストを取得できませんでした。新規の会社は社名・業種が分からないので来月に回します")
        return {}
    z = zipfile.ZipFile(io.BytesIO(r.content))
    text = z.read(next(n for n in z.namelist() if n.lower().endswith(".csv"))).decode("cp932")
    rows = list(csv.reader(io.StringIO(text)))
    head = next(i for i, r in enumerate(rows) if "証券コード" in r)
    h = rows[head]
    ic, iname, isec, il = h.index("証券コード"), h.index("提出者名"), h.index("提出者業種"), h.index("上場区分")
    out = {}
    for r in rows[head + 1:]:
        if len(r) > max(ic, iname, isec, il) and r[ic].strip():
            if listed_only and r[il].strip() != "上場":
                continue
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


def close(a, b):
    return abs(a - b) <= max(0.02 * abs(b), 0.011)


def apply_splits(fin, haito_html):
    """HAITOの data/splits.json(決算後の株式分割)で、1株あたりの値(配当・EPS・BPS)を割り戻す。一度だけ。"""
    path = os.path.join(os.path.dirname(os.path.abspath(haito_html)), "data", "splits.json")
    if not os.path.exists(path):
        return 0
    splits = json.load(open(path, encoding="utf-8"))
    n = 0
    for m in fin.values():
        for code, r in m.items():
            e = splits.get(code)
            if not isinstance(e, dict) or not e.get("効力発生日") or not e.get("比率"):
                continue
            key, ratio = e["効力発生日"], float(e["比率"])
            done = r.setdefault("splits", [])
            if key in done or (r.get("fy") or "") >= key or key > dt.date.today().isoformat():
                continue
            # 「EPS調整済」: 有報の提出前に効力が出た分割で、EPS・BPSは有報の側で分割後の株数になっている。
            # 書いていなければ、当期の 1株配当÷EPS を配当性向と比べて決める(比率ぶんずれていれば調整済み)
            adj = e.get("EPS調整済")
            if adj is None:
                d, p, po = (r.get("div") or [None])[-1], (r.get("eps") or [None])[-1], r.get("payout")
                adj = bool(d and p and p > 0 and po and po > 0 and abs(d / p / po / ratio - 1) < 0.15)
            for f in ("div",) if adj else ("div", "eps"):
                if r.get(f):
                    r[f] = [round(x / ratio, 2) for x in r[f]]
            if r.get("bps") and not adj:
                r["bps"] = round(r["bps"] / ratio, 2)
            done.append(key)
            n += 1
    return n


def attach_special(fin, haito_html):
    """HAITOの data/special-dividends.json(有報の注記から拾った記念配当・特別配当)で、
    記念・特別配当を除いた1株配当 dlo(dl と同じ古い順)と、どの期に含まれていたか sp を付ける。"""
    path = os.path.join(os.path.dirname(os.path.abspath(haito_html)), "data", "special-dividends.json")
    if not os.path.exists(path):
        return 0
    data = json.load(open(path, encoding="utf-8"))
    n = 0
    for m in fin.values():
        for code, r in m.items():
            r.pop("dlo", None)
            r.pop("sp", None)
            e, fy = data.get(code), r.get("fy") or ""
            series = r.get("dl") if r.get("dl") and len(r["dl"]) >= len(r.get("div") or []) else r.get("div")
            if not e or not fy or not series:
                continue
            ords, sp = list(series), {}
            for k, x in (e.get("期") or {}).items():
                back = int(fy[:4]) * 12 + int(fy[5:7]) - int(k[:4]) * 12 - int(k[5:7])
                if back % 12 or not (0 <= back // 12 < len(series)) or not (0 < x.get("割合", 0) < 1):
                    continue
                i = len(series) - 1 - back // 12
                ords[i] = round(series[i] * (1 - x["割合"]), 2)
                sp[k] = round(series[i] - ords[i], 2)
            if sp:
                r["dlo"], r["sp"] = ords, sp
                n += 1
    return n


def attach_irregular(fin, haito_html):
    """HAITOの data/irregular-periods.json(決算期間が12か月でない期と、手で書いた例外)で、
    増配・減配・連続増配を数える系列 dlc(古い順)を付ける。12か月でない期は外し、年間の額がわかる期(div)は置き換える。
    一覧の棒(dlo / dl)はそのまま"""
    path = os.path.join(os.path.dirname(os.path.abspath(haito_html)), "data", "irregular-periods.json")
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}

    def mi(d):
        return int(d[:4]) * 12 + int(d[5:7])
    n = 0
    for m in fin.values():
        for code, r in m.items():
            r.pop("dlc", None)
            e, fy = data.get(code), r.get("fy") or ""
            base = r.get("dlo") or r.get("dl") or r.get("div")
            if not e or not fy or not base:
                continue
            ends = [p for p in e.get("periods", []) if p <= fy]
            if not ends:
                continue
            newer = round((mi(fy) - mi(ends[0])) / 12)
            drop, fix = set(), {}
            for x in e.get("irregular", []):
                if x["end"] > fy or x["end"] not in ends:
                    continue
                k = newer + ends.index(x["end"])
                if x.get("div") is not None:
                    fix[k] = x["div"]
                else:
                    drop.add(k)
            if not drop and not fix:
                continue
            L = len(base)
            r["dlc"] = [fix.get(L - 1 - i, v) for i, v in enumerate(base) if L - 1 - i not in drop]
            n += 1
    return n


def attach_long(fin, haito_html):
    """HAITO(haito-dashboard)の内蔵データにある最大14期の1株配当を、fin の dl(古い順)に付ける。

    HAITO の最新期が fin と同じならそのまま、1期遅れていれば fin の当期を足す。合わなければ付けない。
    """
    html = open(haito_html, encoding="utf-8").read()
    info = json.loads(re.search(r"var CODE_INFO_MAP = (\{.*?\});\n", html).group(1))
    spath = os.path.join(os.path.dirname(os.path.abspath(haito_html)), "tools", "edinet", "state.json")
    hstate = json.load(open(spath)) if os.path.exists(spath) else {}
    n = 0
    for m in fin.values():
        for code, r in m.items():
            r.pop("dl", None)
            # 前の回で差し替えた値は元に戻してから考え直す
            if r.get("div_raw"):
                r["div"] = r.pop("div_raw")
            r.pop("divchk", None)
            d, v = r.get("div"), info.get(code)
            if not d or not v or len(v) < 4 or not v[3]:
                continue
            long = list(reversed(v[3]))
            if close(long[-1], d[-1]):
                pass
            elif len(d) >= 2 and close(long[-1], d[-2]):
                long = long + [d[-1]]
            elif hstate.get(code, "")[:7] == (r.get("fy") or "")[:7] and len(long) >= len(d):
                # 同じ期なのに合わない: 期の途中の株式分割で、有報の1株配当は分割前の中間配当と分割後の期末配当の合計のまま。
                # HAITO は株数の変化から割り戻しているので、そちらの値を使う(生の値は div_raw に残す)
                adj = [round(x, 2) for x in long[-len(d):]]
                r["div_raw"], r["div"] = d, adj
                # 古い期は「生の値 ÷ 整数」になっているはず。そうでなければ割り戻しを人が確かめる
                ks = [d[i] / adj[i] for i in range(len(d) - 1) if adj[i] > 0 and d[i] > 0]
                if not ks or any(round(k) < 1 or abs(k / round(k) - 1) > 0.03 for k in ks):
                    r["divchk"] = 1
            else:
                continue
            if len(long) > len(d):
                r["dl"] = [round(x, 2) for x in long]
                n += 1
    return n


def streak_of(s):
    up = 0
    for k in range(len(s) - 1, 0, -1):
        if s[k] > s[k - 1]:
            up += 1
        else:
            break
    return up


# 予備軍の組み合わせ。配当政策の書き方(テーマ)とチェックシートの項目を組み合わせ、
# 3年前・6年前の有報で「その後に累進配当・DOEを宣言したか」を確かめて、どの時期でも強かったものだけを使う。
# どれか1つに当てはまる会社は6年で10社中約5社、2つ以上なら約6社が宣言していた(ふつうの会社は約2社)
CAND_RULES = {
    "1": ("配当性向の目安×中期経営計画×増配", "配当を「配当性向○%」で約束し、中期経営計画の中で配当を伸ばしている会社。次の計画で「下げない」約束に格上げしやすい"),
    "2": ("配当性向の目安×減配なし×ROEほどほど", "ROEがほどほど(4〜12%)の会社は、利益に連動する配当だとぶれやすいので、配当を安定させる方針(累進配当・DOE)に切り替えやすい。ROEが高い会社は今のままでも配当が増えるので、切り替える必要が薄い"),
    "3": ("配当性向の目安×配当性向高め×PBR1倍割れ", "配当性向が高くなり、利益連動のままだと減益のときに減配しやすい。PBR1倍割れで東証から株価対策も求められている"),
    "4": ("中期経営計画×減配なし×利益が安定", "利益が安定していて減配もせず、中期経営計画で還元を語っている会社。計画を作り直すときに新しい方針を出しやすい"),
    "5": ("自己株買い×PBR1倍割れ×営業CF黒字", "PBR1倍割れで自己株買いなどの株価対策を進めていて、本業でお金も稼げている会社"),
    "6": ("増配を目指すと明記", "すでに「増配」を方針にしている。累進配当の宣言とほぼ同じ方向"),
    "検討": ("導入を検討中", "累進配当・DOEの導入を検討していると有報に書いている"),
}


def nocut_years(ds):
    n = 0
    for i in range(len(ds) - 1, 0, -1):
        if ds[i] and ds[i] > 0 and ds[i] >= ds[i - 1] - 0.005:
            n += 1
        else:
            break
    return n


def candidate(r, ds, po):
    """予備軍なら (段階, 理由の一覧, 減配なし年数) を返す。段階 2: 組み合わせ2つ以上か導入を検討中(強い予備軍)、1: 1つ。
    理由は [組み合わせ, 当てはまった数字, なぜ]"""
    polc = dict(r.get("polc") or [])
    kw = r.get("kw") or []
    nc, up = nocut_years(ds), streak_of(ds)
    meyasu = r.get("pot") is not None
    roe = (r.get("roe") or [None])[-1]
    eps = [v for v in (r.get("eps") or []) if v is not None]
    eps_down = sum(1 for i in range(1, len(eps)) if eps[i] < eps[i - 1]) if len(eps) >= 3 else None
    ocf_neg = sum(1 for v in (r.get("ocf") or []) if v < 0)
    pbr = None
    if r.get("per") and eps and eps[-1] > 0 and r.get("bps"):
        pbr = r["per"] * eps[-1] / r["bps"]
    pbr_low = pbr is not None and 0.5 <= pbr < 1
    pot = "配当性向%s%%を目安" % fmt(r.get("pot"))
    k = []
    def add(key, detail):
        t, why = CAND_RULES[key]
        k.append([t, detail, why])
    if meyasu and "中計" in kw and up >= 1:
        add("1", "%s・配当政策に中期経営計画・%d期連続増配" % (pot, up))
    if meyasu and nc >= 4 and roe is not None and 0.04 <= roe < 0.12:
        add("2", "%s・%d年減配なし・ROE%.1f%%" % (pot, nc, roe * 100))
    if meyasu and po is not None and 40 <= po <= 100 and pbr_low:
        add("3", "%s・いまの配当性向%d%%・PBR%.2f倍" % (pot, round(po), pbr))
    if "中計" in kw and nc >= 4 and eps_down is not None and eps_down <= 1:
        add("4", "配当政策に中期経営計画・%d年減配なし・EPSが下がった年%d回(%d期中)" % (nc, eps_down, len(eps)))
    if "自己株" in kw and pbr_low and r.get("ocf") and ocf_neg == 0:
        add("5", "配当政策に自己株式・PBR%.2f倍・営業CFは%d期ずっと黒字" % (pbr, len(r["ocf"])))
    if "増配を目指す" in polc:
        add("6", polc["増配を目指す"])
    if "導入を検討" in polc:
        add("検討", polc["導入を検討"])
    if not k:
        return None
    return (2 if len(k) >= 2 or "導入を検討" in polc else 1), k, nc


def fmt(v):
    return ("%g" % v) if v is not None else "?"


def track_record(ds, payout):
    """宣言はないが実績が累進配当に近いか。(理由の文) か None

    直近10期(記録が6〜9期ならその全部)で一度も減配せず、半分以上の期で増配していて、
    配当性向が70%以下(無理をしていない)なら「実績は累進」とみる。
    """
    s = ds[-10:]
    if len(s) < 6 or any(v is None or v <= 0 for v in s):
        return None
    if any(s[k] < s[k - 1] - 0.005 for k in range(1, len(s))):
        return None
    ups = sum(1 for k in range(1, len(s)) if s[k] > s[k - 1] + 0.005)
    if ups * 2 < len(s) - 1 or payout is None or payout > 70:
        return None
    pts = 2 if ups == len(s) - 1 else 1 if ups * 10 >= (len(s) - 1) * 7 else 0
    return "%d期減配なし(うち増配%d回)・配当性向%d%%" % (len(s), ups, round(payout)), pts


def track_text(ds, po):
    nc, up0 = nocut_years(ds), streak_of(ds)
    more = "以上" if nc == len(ds) - 1 else ""
    txt = ("%d期%s連続増配" % (up0, "以上" if up0 == len(ds) - 1 else "")) if up0 >= 3 else ("%d年%s減配なし" % (nc, more))
    # 増配が止まった年が据え置きなら、減配していない年数も添える(ひろぎん: 4期連続増配・5年以上減配なし)
    if up0 >= 3 and nc > up0:
        txt += "・%d年%s減配なし" % (nc, more)
    return "%s・配当性向%d%%" % (txt, round(po))


def write_policy(fin):
    """累進配当・DOEの会社と、その予備軍、宣言はないが実績が累進配当に近い会社(実績組)の一覧(policy.json)。一覧ページが読む小さなデータ"""
    rows = []
    for m in fin.values():
        for code, r in m.items():
            d, e = r.get("div") or [], r.get("eps") or []
            # 有報の5期分が分割などで短いときは、長期の記録(割り戻し済み)の直近が同じなら1株利益の期数まで埋める
            lg = r.get("dl") or []
            if d and len(d) < len(e) <= len(lg) and all(abs(a - b) <= 0.011 for a, b in zip(lg[-len(d):], d)):
                d = lg[-len(e):]
            # 記念・特別配当を除いた普通配当で数える(決算期間が12か月でない期を外した dlc があればそれ)
            ds = r.get("dlc") or r.get("dlo") or r.get("dl") or d
            po = round(d[-1] / e[-1] * 100, 1) if d and e and e[-1] > 0 else None
            if r.get("pol"):
                row = {"p": r["pol"], "q": r.get("polq", [])}
            else:
                # 予備軍: 過去の有報で確かめたパターン(★2〜3)と、簡単なサイン(★1)
                got = candidate(r, ds, po)
                if got:
                    st, k, nc = got
                    # 並べ替え: テーマの数が多い順、次に減配なしの年数が長い順
                    row = {"p": [], "k": k, "st": st, "sc": len(k) * 100 + nc}
                    # 予備軍のサインに加えて、実績組の条件も満たしている会社には印を付ける(タブは予備軍だけ)
                    tr = track_record(ds, po)
                    if tr:
                        row["tr"] = track_text(ds, po)
                else:
                    # 実績組: 宣言も予備軍のサインもないが、直近10期で減配せず半分以上の期で増配し、配当性向70%以下
                    tr = track_record(ds, po)
                    if not tr:
                        continue
                    row = {"p": [], "t": track_text(ds, po), "tp": tr[1], "sc": nocut_years(ds)}
            up = streak_of(ds)
            row.update({
                "c": code, "n": r.get("n", ""), "s": r.get("sec", ""), "fy": (r.get("fy") or "")[:7],
                # 一覧の小さな棒も普通配当で(記念配当の翌年が赤くならないように)
                "d": (r["dlo"][-len(d):] if r.get("dlo") and d else d),
                "po": po, "up": up,
            })
            rows.append(row)
            # 記録の始まりまで増配が続いている会社は、それより前も増配だったかもしれない(「N期以上」と出す)
            if up and up == len(ds) - 1:
                rows[-1]["upm"] = 1
            # 1年・5年・10年の増配率(年平均)。長期の記録があればそれを使う。
            # 5年分(6期)ない会社は、5期(4年)の年平均で代わりにし、年数を g5y に残す
            # 増配率も連続増配と同じ系列で(決算期間の例外を直した dlc があればそれ。HAITO の comparableDivs と同じ)
            series = r.get("dlc") or r.get("dlo") or (r.get("dl") if r.get("dl") and len(r["dl"]) > len(d) else d)
            def cagr(n):
                if len(series) > n and series[-1 - n] > 0 and series[-1] > 0:
                    return round((series[-1] / series[-1 - n]) ** (1 / n) - 1, 4)
                return None
            g = {"g1": cagr(1), "g5": cagr(5), "g10": cagr(10), "g15": cagr(15)}
            if g["g5"] is None and cagr(4) is not None:
                g["g5"], g["g5y"] = cagr(4), 4
            rows[-1].update({k: v for k, v in g.items() if v is not None})
    rows.sort(key=lambda x: x["c"])
    out = {"built": dt.date.today().isoformat(), "checked": sum(1 for m in fin.values() for r in m.values() if r.get("pc")),
           "rows": rows}
    with open(os.path.join(ROOT, "policy.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return sum(1 for x in rows if x["p"])


def cmd_update(days, only_missing=False):
    fin = load_fin()
    filings = recent_filings(days)
    todo = []
    for code, x in filings.items():
        old = fin.get(code[0], {}).get(code)
        if old and (only_missing or old.get("fy", "") >= x["periodEnd"]):
            continue
        todo.append((code, x))
    print("有報", len(filings), "件のうち、期が新しい会社", len(todo), "件", flush=True)
    todo = [(c, x) for c, x in todo if re.match(r"^[1-9][0-9A-Z]{3}$", c)]
    names = code_list(listed_only=only_missing) if any(not fin.get(c[0], {}).get(c) for c, _ in todo) else {}
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


def cmd_refetch(days, codes=None):
    """読み方を直したあと、最新の有報を読み直して数字を入れ直す(codes を指定すればその会社だけ)"""
    fin = load_fin()
    filings = recent_filings(days)
    todo = [(c, x) for c, x in filings.items() if fin.get(c[0], {}).get(c) and (not codes or c in codes)]
    print("有報", len(todo), "件を読み直します", flush=True)
    n = 0
    with ThreadPoolExecutor(4) as ex:
        for (code, x), parsed in zip(todo, ex.map(lambda t: download(t[1]["docID"]), todo)):
            if not parsed or not parsed.get("fyend"):
                continue
            old = fin[code[0]][code]
            new = record(parsed, old.get("n"), old.get("sec", ""))
            # 長期の配当・記念配当・PERなど、別の手順で足した値は残す(longdiv で付け直す前提でも、消えたままにしない)
            for k in ("dl", "dlo", "sp", "splits", "per", "pot", "polc", "kw", "q"):
                if k in old and k not in new:
                    new[k] = old[k]
            fin[code[0]][code] = new
            n += 1
    save_fin(fin)
    return n


def cmd_payout(days):
    """最新の有報の配当政策から「配当性向○%」型の方針と予備軍の手がかりだけを読み直して足す(埋め合わせ用)"""
    fin = load_fin()
    filings = recent_filings(days)
    todo = [(c, x) for c, x in filings.items() if fin.get(c[0], {}).get(c) and fin[c[0]][c].get("fy", "") <= x["periodEnd"]]
    print("有報", len(todo), "件を読みます", flush=True)
    n = 0
    with ThreadPoolExecutor(4) as ex:
        for (code, x), parsed in zip(todo, ex.map(lambda t: download(t[1]["docID"]), todo)):
            if not parsed:
                continue
            r = fin[code[0]][code]
            r.pop("pot", None); r.pop("potq", None); r.pop("polc", None); r.pop("pol", None); r.pop("polq", None)
            if parsed.get("policy") is not None:
                r["pc"] = 1
                if parsed["policy"][0]:
                    r["pol"], r["polq"] = parsed["policy"]
            if parsed.get("potarget"):
                r["pot"], r["potq"] = parsed["potarget"]
                n += 1
            if parsed.get("candtext") and not r.get("pol"):
                r["polc"] = parsed["candtext"]
            r.pop("kw", None)
            if parsed.get("kw"):
                r["kw"] = parsed["kw"]
            if parsed.get("net"):
                r["net"] = round(parsed["net"] / 1e8)
            r.pop("per", None)
            if parsed.get("per") and parsed["per"] > 0:
                r["per"] = round(parsed["per"], 2)
    save_fin(fin)
    return n


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("policy")
    rf = sub.add_parser("refetch")
    rf.add_argument("--days", type=int, default=400)
    rf.add_argument("--codes", default="", help="カンマ区切り。空なら全社")
    po = sub.add_parser("payout")
    po.add_argument("--days", type=int, default=400)
    lg = sub.add_parser("longdiv")
    lg.add_argument("--haito", required=True, help="haito-dashboard の index.html")
    u = sub.add_parser("update")
    u.add_argument("--days", type=int, default=45)
    u.add_argument("--only-missing", action="store_true",
                   help="まだ無い上場会社(名証・福証・札証だけの会社など)だけ足す。--days 400 で1年分を見る")
    d = sub.add_parser("doc")
    d.add_argument("doc_id")
    a = ap.parse_args()
    if a.cmd == "update":
        cmd_update(a.days, a.only_missing)
    elif a.cmd == "refetch":
        print("読み直した会社", cmd_refetch(a.days, [c for c in a.codes.split(",") if c]))
    elif a.cmd == "payout":
        print("配当性向○%型の方針の会社", cmd_payout(a.days))
    elif a.cmd == "policy":
        print(write_policy(load_fin()))
    elif a.cmd == "longdiv":
        fin = load_fin()
        print("決算後の分割で割り戻した会社", apply_splits(fin, a.haito))
        print("長期の1株配当を付けた会社", attach_long(fin, a.haito))
        print("記念・特別配当を除いた系列を付けた会社", attach_special(fin, a.haito))
        print("決算期間が12か月でない期を外した系列を付けた会社", attach_irregular(fin, a.haito))
        save_fin(fin)
        print("累進配当・DOEの会社", write_policy(fin))
    else:
        print(json.dumps(record(download(a.doc_id), "", ""), ensure_ascii=False))


if __name__ == "__main__":
    main()
