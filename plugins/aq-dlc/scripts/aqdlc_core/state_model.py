"""run の状態（state.json の辞書）の定数と、読み取り・小さな書き換えの関数。ファイルを読み書きしない。

state.py から本文を変えずに移した。"""
import re

from . import gate as G
from .util import (fingerprint, now_iso)


SCHEMA = 2


# E-AQDL で追加した機能の版（0 = AQ-DLC v0.5 以前に始まった run。新しい承認・同意の条件は求めない）
EAQDL = 1


STATUSES = ("in_progress", "pr_ready", "complete", "closed", "aborted")


ROLES = ("requirements", "design", "implementer", "reviewer")


ROLE_LABEL = {"requirements": "要求分析（aq-dlc:requirements）", "design": "設計（aq-dlc:design）",
              "implementer": "実装（aq-dlc:implementer）", "reviewer": "独立レビュー（aq-dlc:reviewer）"}


ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")


def bump(state):
    state["seq"] = int(state.get("seq", 0)) + 1
    return state["seq"]


def human_event(state, kind, text, parsed=True):
    """人間の返答の記録。parsed=False は回答部分を取り出せなかったもの（承認の判定には使わない）。"""
    seq = bump(state)
    state["human_events"].append({"seq": seq, "at": now_iso(), "kind": kind, "text": (text or "")[:2000],
                                  "parsed": bool(parsed)})
    state["human_events"] = state["human_events"][-50:]
    return seq


def last_human_seq(state):
    return max([e["seq"] for e in state.get("human_events", [])] or [0])


# ------------------------------------------------------------------ graph
def iter_nodes(state):
    for r in state.get("requirements", []):
        yield "requirement", r, None
        for a in r.get("acceptance", []):
            yield "acceptance", a, r["id"]
            for v in a.get("verifications", []):
                yield "verification", v, a["id"]
        for k in r.get("risks", []):
            yield "risk", k, r["id"]
            for v in k.get("verifications", []):
                yield "verification", v, k["id"]
    for key, kind in (("questions", "question"), ("assumptions", "assumption"),
                      ("out_of_scope", "out_of_scope"), ("decisions", "decision"),
                      ("escalations", "escalation")):
        for n in state.get(key, []):
            yield kind, n, None


def id_index(state):
    idx, dups = {}, []
    for kind, node, parent in iter_nodes(state):
        nid = node.get("id")
        if nid in idx:
            dups.append(nid)
        else:
            idx[nid] = (kind, node, parent)
    return idx, sorted(set(dups))


def iter_verifications(state):
    for r in state.get("requirements", []):
        for p in r.get("acceptance", []) + r.get("risks", []):
            for v in p.get("verifications", []):
                yield r, p, v


def deferred_ok(v, cfg):
    """人間の承認のもとで「未確認のまま進める」とされ、その後に検証の定義が変わっていないか。"""
    d = v.get("deferred") or {}
    return bool(d) and d.get("fp") == G.verification_fp(v, cfg)


def deferred_list(state, cfg):
    return [v["id"] for _r, _p, v in iter_verifications(state) if deferred_ok(v, cfg)]


def graph_fp(state):
    def vdef(v):
        return {k: v.get(k) for k in ("id", "method", "command", "checks", "manual", "after_merge",
                                       "level", "test", "timeout_sec")}
    return fingerprint([[r["id"], r.get("text"), bool(r.get("critical")),
                         [[a["id"], a.get("text"), [vdef(v) for v in a.get("verifications", [])]]
                          for a in r.get("acceptance", [])],
                         [[k["id"], k.get("text"), [vdef(v) for v in k.get("verifications", [])]]
                          for k in r.get("risks", [])]]
                        for r in state.get("requirements", [])])


PHASE_OF_ROUTE = {"counterfactual": "反証テスト", "requirements": "要求整理", "human": "人間の確認", "orchestrator": "差し戻しの対応",
                  "design": "設計", "implementer": "実装・修正", "gate": "Quality Gate",
                  "reviewer": "独立レビュー", "memory": "Memory 整理", "ci": "CI の確認",
                  "after_merge": "マージ後の確認"}


AGENT_CALL = ("Agent ツールで {agent} を呼ぶ（subagent_type: \"{agent}\"）。prompt には目的を1〜2行で書けばよい。"
              "仕様・設計・指摘などの引き継ぎ情報はエンジンが自動で渡し、終了時に成果物を検証して記録する。")
