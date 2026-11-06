# 欠陥検出ベンチマーク（PERF-01）

独立レビュー担当が、埋め込んだ欠陥をどれだけ見つけるか（Recall）、欠陥でないものをどれだけ指摘するか（Precision）を測るためのベンチマークです。手順と数え方は [PROTOCOL.md](PROTOCOL.md) を、v0.6.6 の基準値は [results/v0.6.6-baseline/ANALYSIS.md](results/v0.6.6-baseline/ANALYSIS.md) を見てください。

| ファイル | 内容 |
|---|---|
| `app/` | 参照コード（欠陥なし）と、見えるテスト |
| `cases.py` | 40ケース（欠陥30・欠陥なし10）の正解と、再現テスト。レビュー担当には渡さない |
| `check_cases.py` | ケースの妥当性を機械で確かめる |
| `run_docs.py` | 全ケースで共通の仕様書・設計書・要求 |
| `run_bench.py` | build（run をレビューの直前まで進める）・review・judge・report |
| `report.py` | 集計（TP・FP・FN・precision・recall・費用・時間） |
| `results/` | 計測の生データ・条件・判定の手での確認・分析 |
