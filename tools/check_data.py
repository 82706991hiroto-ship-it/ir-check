#!/usr/bin/env python3
"""数字の点検。毎週の更新のあと(と手元での変更のあと)に動かす。

1. 正解値との照合(tests/golden.json): 人が公式の書類で確かめた値と食い違えば、終了コード1で止める(公開しない)。
2. データどうしの突き合わせ: チェックシートの有報データと HAITO の内蔵データ(1株配当)を比べ、
   食い違いや説明のつかない急な変化を checks.json に書く。アプリは該当銘柄に「数字を要確認」を出す。

  python3 tools/check_data.py --haito ../haito-dashboard/index.html
"""
import argparse, datetime as dt, glob, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_fin():
    fin = {}
    for f in glob.glob(os.path.join(ROOT, "fin", "*.json")):
        fin.update(json.load(open(f, encoding="utf-8")))
    return fin


def js_map(html, name):
    m = re.search(r"var %s = (\{.*?\});\n" % name, html)
    return json.loads(m.group(1)) if m else {}


def close(a, b, tol=0.02):
    return a is not None and b is not None and abs(a - b) <= max(abs(b) * tol, 0.011)


def golden(fin, info, special_map):
    gold = json.load(open(os.path.join(ROOT, "tests", "golden.json"), encoding="utf-8"))
    bad = []
    for code, g in gold.items():
        if code.startswith("_"):
            continue
        r, v = fin.get(code) or {}, info.get(code)
        name = g.get("名前", code)
        def ng(what, got, want):
            bad.append("%s %s: %s が %s(正解 %s)" % (code, name, what, got, want))
        if "直近の1株配当" in g:
            want = g["直近の1株配当"]
            got_h = v[3][0] if v and len(v) > 3 and v[3] else None
            got_f = (r.get("div") or [None])[-1]
            if not close(got_h, want): ng("HAITOの直近の1株配当", got_h, want)
            if not close(got_f, want): ng("チェックシートの直近の1株配当", got_f, want)
        if "普通配当の直近2期" in g:
            got = (r.get("dlo") or [])[-2:]
            if len(got) != 2 or not all(close(a, b) for a, b in zip(got, g["普通配当の直近2期"])):
                ng("チェックシートの普通配当(記念・特別配当を除く)", got, g["普通配当の直近2期"])
            if str(code) not in special_map: ng("HAITOの記念・特別配当", "なし", g.get("記念・特別配当"))
        if "連結の配当性向" in g:
            d, e = (r.get("div") or [None])[-1], (r.get("eps") or [None])[-1]
            got = d / e if d and e else None
            if not close(got, g["連結の配当性向"], 0.03): ng("連結の配当性向(1株配当÷EPS)", got, g["連結の配当性向"])
        if "配当性向の目安" in g and not close(r.get("pot"), g["配当性向の目安"]):
            ng("配当方針の配当性向の目安", r.get("pot"), g["配当性向の目安"])
        if "直近の1株利益" in g and not close((r.get("eps") or [None])[-1], g["直近の1株利益"]):
            ng("直近の1株利益", (r.get("eps") or [None])[-1], g["直近の1株利益"])
    return bad


def cross(fin, info, split_map, special_map):
    """{code: [要確認の理由]}"""
    out = {}
    for code, r in fin.items():
        msgs = []
        v = info.get(code)
        h = v[3] if v and len(v) > 3 and v[3] else None
        d = r.get("div") or []
        # HAITOの内蔵データ(有報14期)とチェックシートの有報データの1株配当が、同じ期でも1期ずれでも合わない
        if h and d and h[0] > 0 and d[-1] > 0 and not close(h[0], d[-1], 0.03) and not (len(d) > 1 and close(h[0], d[-2], 0.03)) and not (len(h) > 1 and close(h[1], d[-1], 0.03)):
            msgs.append("1株配当がデータ間で合わない(%s円と%s円)" % (h[0], d[-1]))
        # 株式分割の取りこぼし: 1株配当と1株利益が同じ期に、ほぼ同じ割合で 1/2・1/3… に下がっている
        dd, ee = r.get("div") or [], r.get("eps") or []
        for i in range(1, min(len(dd), len(ee))):
            a, b, ea, eb = dd[i - 1], dd[i], ee[i - 1], ee[i]
            if not (a > 0 and b > 0 and ea > 0 and eb > 0):
                continue
            k = round(a / b)
            if 2 <= k <= 10 and abs(a / b / k - 1) < 0.08 and abs((ea / eb) / k - 1) < 0.25:
                msgs.append("1株配当と1株利益が同じ期に約1/%dになっている(%s→%s円)。株式分割の割り戻し漏れの可能性" % (k, a, b))
                break
        # 1株利益・1株純資産・ROE は同じ決算から出るので、ROE ≒ 1株利益 ÷ 1株純資産(期中平均との差で多少ずれる)。
        # 大きく外れていれば、基準(連結/単体・日本基準/IFRS)や株式分割の割り戻しの取り違えを疑う
        e, b, roe = (r.get("eps") or [None])[-1], r.get("bps"), (r.get("roe") or [None])[-1]
        if e and b and roe and e > 0 and b > 0 and roe > 0.02:
            q = (e / b) / roe
            if q < 0.35 or q > 3:
                msgs.append("1株利益÷1株純資産(%.1f%%)とROE(%.1f%%)が合わない。基準や分割の取り違えの可能性" % (e / b * 100, roe * 100))
        if e is not None and roe is not None and abs(roe) > 0.01 and (e > 0) != (roe > 0):
            msgs.append("1株利益とROEの符号が合わない")
        eq = (r.get("eq") or [None])[-1]
        if eq is not None and not (0 < eq <= 1):
            msgs.append("自己資本比率が0〜100%%の外(%s)" % eq)
        if r.get("divchk"):
            msgs.append("期の途中の株式分割で1株配当を割り戻した値を確かめてください(有報の値 %s)" % r.get("div_raw"))
        if msgs:
            out[code] = msgs
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--haito", required=True, help="haito-dashboard の index.html")
    a = ap.parse_args()
    html = open(a.haito, encoding="utf-8").read()
    info = json.loads(re.search(r"var CODE_INFO_MAP = (\{.*?\});\n", html).group(1))
    split_map, special_map = js_map(html, "SPLIT_MAP"), js_map(html, "SPECIAL_MAP")
    fin = load_fin()

    flags = cross(fin, info, split_map, special_map)
    with open(os.path.join(ROOT, "checks.json"), "w", encoding="utf-8") as f:
        json.dump({"built": dt.date.today().isoformat(), "flags": dict(sorted(flags.items()))}, f, ensure_ascii=False, separators=(",", ":"))
    print("要確認の銘柄", len(flags))

    bad = golden(fin, info, special_map)
    for b in bad:
        print("正解値と食い違い:", b)
    if bad:
        sys.exit(1)
    print("正解値との照合: すべて一致")


if __name__ == "__main__":
    main()
