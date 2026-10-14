# 作業メモ

作業の約束事・情報源の制限・いま決まっていないこと・最近やったことは、**HAITO側のメモにまとめてある**。
最初に `../haito-dashboard/NOTES.md`(同じフォルダに無ければ https://github.com/82706991hiroto-ship-it/haito-dashboard/blob/main/NOTES.md)を読むこと。

このリポジトリ固有の点:
- `fin/N.json` は `tools/edinet_fin.py` が有報から作る。読み方を直したら `python3 tools/edinet_fin.py refetch --days 400 --codes コード,コード` で入れ直す(別の手順で足した値は消さない)。
- データを変えたら `python3 tools/check_data.py --haito ../haito-dashboard/index.html` で正解値(`tests/golden.json`)と照合してから公開する。
- 予備軍の判定は `tools/edinet_fin.py` の `candidate()`(6つの組み合わせ、★★/★)。
