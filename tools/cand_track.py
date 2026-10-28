#!/usr/bin/env python3
"""予備軍・実績の会社が、その後ほんとうに累進配当・DOEを宣言したかを数えるための記録と集計。

  python3 tools/cand_track.py snapshot   # その月の一覧を history/cands-YYYY-MM.json に残す(月に1回だけ。毎週の更新で呼ぶ)
  python3 tools/cand_track.py report     # 残した月ごとに、今の policy.json で宣言済みになった会社の割合を出す

宣言は有報の配当政策で見ている(policy.json の p)ので、決算短信や中期経営計画で先に宣言した会社は、次の有報が出るまで数に入らない。
"""
import datetime as dt, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "history")


def load_policy():
    return json.load(open(os.path.join(ROOT, "policy.json"), encoding="utf-8"))


def kind(r):
    return "decl" if r.get("p") else "cand" if r.get("k") else "track" if r.get("t") else ""


def snapshot(force=False):
    pol = load_policy()
    month = dt.date.today().strftime("%Y-%m")
    path = os.path.join(HIST, "cands-%s.json" % month)
    if os.path.exists(path) and not force:
        print("今月の記録はもうあります:", os.path.relpath(path, ROOT))
        return
    rows = []
    for r in pol["rows"]:
        k = kind(r)
        if k in ("cand", "track"):
            x = {"c": r["c"], "n": r.get("n", ""), "kind": k}
            if k == "cand":
                x["st"] = r.get("st", 1)
                x["k"] = [t[0] for t in r.get("k", [])]
                if r.get("tr"):
                    x["tr"] = 1
            rows.append(x)
    decl = sorted(r["c"] for r in pol["rows"] if r.get("p"))
    os.makedirs(HIST, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": dt.date.today().isoformat(), "built": pol.get("built"), "declared": decl, "rows": rows},
                  f, ensure_ascii=False, separators=(",", ":"))
    print("記録しました:", os.path.relpath(path, ROOT), "予備軍", sum(1 for x in rows if x["kind"] == "cand"),
          "社・実績", sum(1 for x in rows if x["kind"] == "track"), "社")


def report():
    pol = load_policy()
    now = {r["c"]: r.get("p") or [] for r in pol["rows"] if r.get("p")}
    files = sorted(f for f in os.listdir(HIST) if f.startswith("cands-")) if os.path.isdir(HIST) else []
    if not files:
        print("まだ記録がありません")
        return
    print("今の policy.json:", pol.get("built"), "時点の宣言済み", len(now), "社")
    for f in files:
        snap = json.load(open(os.path.join(HIST, f), encoding="utf-8"))
        print("\n%s の一覧(%s)" % (snap["date"], f))
        groups = [("予備軍 ★★", lambda x: x["kind"] == "cand" and x.get("st") == 2),
                  ("予備軍 ★", lambda x: x["kind"] == "cand" and x.get("st") != 2),
                  ("予備軍のうち実績あり", lambda x: x["kind"] == "cand" and x.get("tr")),
                  ("実績", lambda x: x["kind"] == "track")]
        for label, fn in groups:
            g = [x for x in snap["rows"] if fn(x)]
            hit = [x for x in g if x["c"] in now]
            rui = sum(1 for x in hit if "累進配当" in now[x["c"]])
            doe = sum(1 for x in hit if "DOE" in now[x["c"]])
            pct = len(hit) / len(g) * 100 if g else 0
            print("  %-12s %4d社中 %3d社が宣言(%.1f%%) 累進%d・DOE%d %s" % (
                label, len(g), len(hit), pct, rui, doe,
                " ".join("%s%s" % (x["c"], x["n"]) for x in hit[:8]) + (" …" if len(hit) > 8 else "")))
        # 組み合わせ(テーマ)ごと
        themes = {}
        for x in snap["rows"]:
            for t in x.get("k", []):
                themes.setdefault(t, []).append(x)
        if themes:
            print("  テーマ別:")
            for t, g in sorted(themes.items(), key=lambda kv: -len(kv[1])):
                hit = sum(1 for x in g if x["c"] in now)
                print("    %s: %d社中 %d社(%.1f%%)" % (t, len(g), hit, hit / len(g) * 100))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot":
        snapshot("--force" in sys.argv)
    elif cmd == "report":
        report()
    else:
        print(__doc__)
