# PERF-01 パイロット: 通常の Claude Code と E-AQDL の全工程の比較

要件書の PERF-01 を小さく試すための手順（1題材 × 各1回）。費用と時間を実測してから、本番の回数を決める。
使い方と、比べ方の前提は `run_workflow.py` の先頭に書いた。

- 題材: `starter/`（仕様書 `docs/spec.md`・インターフェースの契約 `docs/contract.md`・用意済みの `notes/db.py`・`notes/errors.py`）
- 隠しテスト: `bench/review/app_clean/tests`（63件）と repro 38件（実装担当にも、通常の Claude Code にも見せない）
- 人の役は台本（決まった返答）。E-AQDL の仕様の承認は、この返答（実際の利用者の発言として届く）で行う
- 未測定: 独立した評価者による保守性・セキュリティの評価（パイロットでは隠しテストだけ）
