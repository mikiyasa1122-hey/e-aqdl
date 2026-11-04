# E-AQDL 比較実験の記録

Claude Code をヘッドレス（`claude -p`）で動かし、同じお題で条件を変えて比べた記録です。各条件1回だけなので、結果は「傾向」として扱ってください。改善の証明ではありません。

| 実験 | 問い | 結果の要約 |
|---|---|---|
| 01 | 詳しい仕様を渡したとき、品質に差は出るか | 差は出なかった（下記） |
| 02 | 短いお題で、セキュリティに差は出るか | 4条件とも重大な問題なし（[exp02-results.md](exp02-results.md)） |
| 03 | 短いお題で、仕様が依頼者の意図どおりになるか | 質問しない条件は 2〜3/8、質問する条件は 8/8（[exp03-results.md](exp03-results.md)） |
| 04 | 確認できない条件で、完了報告は正直か | 全条件が正直（[exp04-07-results.md](exp04-07-results.md)） |
| 05 | 途中の仕様変更で、意図どおりに変わるか | 質問しない条件は 5.5〜6.5/7、質問する条件は 7/7（[exp05-results.md](exp05-results.md)） |
| 07 | 本番 DB と秘密情報がある状況で、危ない操作をするか | 全条件で本番は無傷・漏えいなし（[exp04-07-results.md](exp04-07-results.md)） |
| 11 | リスクに応じた工程の選択（auto）で費用が下がるか | 低リスクのお題で平均 −21%（お題により −47%〜+6%）。品質は同等。lite は一度も選ばれず（[exp11-results.md](exp11-results.md)） |
| 12 | lite（設計とレビューを省く）で品質は落ちないか | 隠しテストは全件合格で品質は同じ。時間 −25%、費用はほぼ同じ（費用は人とのやりとりの回数で決まる）（[exp12-results.md](exp12-results.md)） |
| 13 | 人への確認を1回にまとめる（判断シート）と費用は下がるか | 返答 2.9→1.2回、費用 −57%、時間 −53%、品質は同じ（[exp13-results.md](exp13-results.md)） |

## 実験01（詳しい仕様）

- お題: [prompts/exp01-task.md](prompts/exp01-task.md)
- 計画: [exp01-plan.md](exp01-plan.md)
- 隠しテスト: [harness/exp01_test_acceptance.py](harness/exp01_test_acceptance.py)（31件）

| 条件 | 時間 | 費用 | 隠しテスト | 自前のテスト | Mutation 検出率 |
|---|---|---|---|---|---|
| ① 普通 | 38秒 | $0.13 | 31/31 | なし | ― |
| ② テストを書いて | 58秒 | $0.18 | 31/31 | 12件 | 0.866（71/82） |
| ③ E-AQDL・Memory なし | 約29分 | $18.89 | 31/31 | 106件 | 0.85（102/120） |
| ④ E-AQDL・Memory あり | 中止 | ― | ― | ― | ― |

- 仕様が詳しいと、差が出る余地がありませんでした。
- ④は実行の制限時間で止まり、その後は実験02に切り替えたため中止しました。

## 実験の仕組み（harness/）

- [driver.py](harness/driver.py): 実験01・02 用です。決まった返答（[prompts/exp01-02-replies.md](prompts/exp01-02-replies.md)）を自動で返します。
- [driver3.py](harness/driver3.py): 実験03 用です。隠れた意図（[exp03-intent.md](exp03-intent.md)）を持つ依頼者役（[simulator_prompt.md](harness/simulator_prompt.md)）が返答します。
- [evaluate.py](harness/evaluate.py): 実験01 用です。隠しテストを実行し、成果物自身のテストの Mutation 検出率を測ります。
- 実験02・03 の採点は、条件名を伏せた検査役のエージェントが、成果物を実際に起動して行いました。チェックリストは [exp02-attack-checklist.md](exp02-attack-checklist.md) と [exp03-intent-check.md](exp03-intent-check.md) です。

スクリプト内のパスは、実験を行った環境のものです。モデルはすべてヘッドレス実行の既定（Claude Sonnet 5.5）。

`fixtures/` は実験04・07の作業開始時のリポジトリ。exp07 の `.env` の決済キーは実在しない偽の値です。
