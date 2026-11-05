"""1回の gate の実行手順（コーディネーター）。

cli.cmd_gate（引数・前提条件・ロック・保存・表示）から呼ぶ。ここは次の順に実行し、結果をまとめて返す:
  1. 実行開始時のコード・定義の指紋を取る
  2. 前回の差分カバレッジのレポートを消す（このあとにあれば、この gate で作られたもの）
  3. チェックと検証を実行する（gate.run_gate）
  4. 隠し受け入れテスト（holdout）を実行する
  5. 差分カバレッジを測り、verdict で合否を決める
  6. 実行後の指紋を取り、gate の実行中の変更を判定する
合否の規則は verdict.py、コマンドの実行は gate.py / holdout.py、差分の測定は quality.py にある。
品質の検査を足すときは、ここ（実行の順番）と verdict（合否）と実行側だけを変える。

holdout.py が gate.py を import するため、この手順は gate.py ではなく別のモジュールに置く（循環を作らない）。
"""
import os
import time

from . import gate as G
from . import holdout as HO
from . import quality as Q
from . import verdict as V


def run(project, state, cfg, run_dir, stamp, iter_verifications, after_merge, load_cfg):
    """戻り値: {"results", "ok", "before", "after", "fps", "holdout_fp", "sec", "modified", "lines"}

    load_cfg: 実行後の設定を読み直す関数（gate の実行中の設定の変更を検出する）。
    lines: 表示用の行（表示は呼び出し側が行う）。
    """
    before = G.snapshot(project, cfg, fresh=True)
    fps = {"checks": G.checks_fp(cfg), "config": G.config_fp(cfg, project),
           "verifications": {v["id"]: G.verification_fp(v, cfg) for _r, _p, v in iter_verifications(state)}}
    dc_enabled = Q.config_of(cfg)["enabled"]
    _removed, inv_err = Q.invalidate_report(project, cfg) if dc_enabled else (False, None)
    t0 = time.time()
    results, _checks_ok = G.run_gate(project, state, cfg, iter_verifications, os.path.join(run_dir, "gate"), stamp,
                                     after_merge, snap=before)
    results["gate_run"] = stamp
    holdout_fp = HO.fingerprint(project, state) if HO.enabled(cfg) else None
    if HO.enabled(cfg) and HO.items(state):
        results["holdout"] = HO.run_all(project, state, cfg, stamp)
    lines = []
    if dc_enabled:
        c = Q.config_of(cfg)
        measure, ev = Q.measure_diff_coverage(project, state, cfg, before, inv_err)
        if ev:
            ev["gate_run"] = stamp
        dc = V.diff_coverage(measure, c["min"], c["report"], ev)
        results["diff_coverage"] = dc
        lines.append(f"  {V.LABEL[V.status_of(dc)]} 差分カバレッジ"
                     + (f": {dc['percent']}%（基準 {dc['min']:g}%）" if dc.get("percent") is not None else "")
                     + (f" — {dc['reason']}" if dc.get("reason") and not V.passed(dc) else ""))
    ok = V.overall(results, cfg.get("checks"))
    sec = round(time.time() - t0, 1)
    cfg_after = load_cfg()
    after = G.snapshot(project, cfg, fresh=True)
    return {"results": results, "ok": ok, "before": before, "after": after, "fps": fps, "cfg_after": cfg_after,
            "holdout_fp": holdout_fp, "sec": sec, "lines": lines}


def modified_during(before, after, fps, cfg_after, project, verifications_after):
    """gate の実行中に変わったもの。なければ None（純粋関数ではないが、ファイルも外部コマンドも使わない）。"""
    c, a, r = G.snapshot_diff(before, after)
    changed_defs = []
    if G.checks_fp(cfg_after) != fps["checks"]:
        changed_defs.append("チェックの定義")
    if G.config_fp(cfg_after, project) != fps["config"]:
        changed_defs.append("設定")
    if {v["id"]: G.verification_fp(v, cfg_after) for v in verifications_after} != fps["verifications"]:
        changed_defs.append("検証条件")
    if not (c or a or r or changed_defs):
        return None
    return {"files": c + a + r, "changed": c, "added": a, "removed": r, "definitions": changed_defs,
            "summary": G.describe_diff(before, after) or "、".join(changed_defs)}
