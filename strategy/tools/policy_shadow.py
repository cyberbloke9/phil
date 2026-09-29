#!/usr/bin/env python3
"""Shadow verdict of strategy/policy.py on live bets. Non-blocking by design.

Why (2026-09-29 audit of the bet-conversion layer): core/ledger.py place
enforces only the protected caps. The floors in strategy/risk.json and the
replay-validated rules in strategy/policy.py are enforced by nothing but the
cycle agent reading prose, and only 16 of the 55 live bets to date would have
passed policy.decide(). Whether that gap costs money is unmeasured because
no row records what the policy would have said. This tool records it.

It never blocks a bet. A hard reject belongs in core/ledger.py and is the
operator's call (journal/proposals.md). This is the shadow field that makes
that call decidable on data instead of argument.

Usage:
  python3 strategy/tools/policy_shadow.py check --est-prob P --ask A --bid B
      Pre-flight for one candidate before `core/ledger.py place`. Prints one
      JSON line: {"verdict": allow|flip|reject, "reasons": [...], ...}.
      Put `shadow:<verdict>` in the bet's --rationale so retros can grade it.
      Exit code is always 0.

  python3 strategy/tools/policy_shadow.py ledger
      Re-derive the verdict for every row of journal/ledger.jsonl, write
      strategy/policy-shadow.jsonl (deterministic, one row per bet, fully
      rewritten each run) and print a summary: allowed vs rejected counts and
      settled P&L of each group. Cheap enough to run every retro.

Field mapping (mirrors core/replay.py's fill model): the ledger always buys
the recorded `outcome` token at `entry_price` = best ask, so the live side is
"yes" in policy.py terms; `best_bid_at_entry` is the bid. A policy answer of
"no" means the policy would have taken the OTHER side ("flip"); None means it
would not have bet ("reject").
"""
import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEDGER = ROOT / "journal" / "ledger.jsonl"
OUT = ROOT / "strategy" / "policy-shadow.jsonl"
POLICY_PATH = ROOT / "strategy" / "policy.py"


def load_policy():
    spec = importlib.util.spec_from_file_location("shadow_policy", POLICY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reasons_for(policy, est, ask, bid):
    """Which written rule(s) the live 'yes' side fails. Empty list = passes."""
    out = []
    if ask is None or bid is None:
        return ["no-book"]
    if round(ask - bid, 4) > policy.MAX_SPREAD:
        out.append(f"spread {round(ask - bid, 4)} > {policy.MAX_SPREAD}")
    edge = round(est - ask, 4)
    if edge < policy.MIN_EDGE:
        out.append(f"edge {edge} < {policy.MIN_EDGE}")
    elif edge > policy.MAX_EDGE:
        out.append(f"edge {edge} > {policy.MAX_EDGE} (overconfidence band)")
    if not policy.MIN_PRICE <= ask <= policy.MAX_PRICE:
        out.append(f"price {ask} outside [{policy.MIN_PRICE}, {policy.MAX_PRICE}]")
    lo, hi = policy.DEAD_ZONE
    if lo <= ask < hi:
        out.append(f"price {ask} in dead zone [{lo}, {hi})")
    return out


def verdict(policy, est, ask, bid):
    row = {"est_prob": est, "best_ask_at_record": ask, "best_bid_at_record": bid}
    d = policy.decide(row)
    if d is None:
        v = "reject"
    elif d["side"] == "yes":
        v = "allow"
    else:
        v = "flip"
    return {
        "verdict": v,
        "policy_decision": d,
        "reasons": reasons_for(policy, est, ask, bid) if v != "allow" else [],
        "policy_rev": policy_rev(),
    }


def policy_rev():
    """Short identity of the policy file so a verdict can be re-derived."""
    import hashlib
    return hashlib.sha1(POLICY_PATH.read_bytes()).hexdigest()[:8]


def cmd_check(args):
    policy = load_policy()
    v = verdict(policy, args.est_prob, args.ask, args.bid)
    v.update({"est_prob": args.est_prob, "ask": args.ask, "bid": args.bid})
    print(json.dumps(v))


def cmd_ledger(args):
    policy = load_policy()
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    out_rows, groups = [], {"allow": [], "flip": [], "reject": []}
    for r in rows:
        v = verdict(policy, r["est_prob"], r.get("entry_price"), r.get("best_bid_at_entry"))
        shadow = {
            "id": r["id"], "ts": r["ts"], "status": r["status"],
            "pnl_usd": r.get("pnl_usd"), "edge_class": r.get("edge_class"),
            "category": r.get("category"), "entry_price": r.get("entry_price"),
            "est_prob": r["est_prob"], **v,
        }
        out_rows.append(shadow)
        groups[v["verdict"]].append(shadow)
    OUT.write_text("".join(json.dumps(s) + "\n" for s in out_rows))

    def summ(name, g):
        settled = [s for s in g if s["status"] in ("won", "lost")]
        wins = sum(1 for s in settled if s["status"] == "won")
        pnl = round(sum(s["pnl_usd"] or 0 for s in settled), 2)
        print(f"  {name:7s} n={len(g):3d} settled={len(settled):3d} "
              f"W/L={wins}/{len(settled) - wins} pnl=${pnl:+.2f}")

    print(f"policy shadow over {len(rows)} ledger rows (policy {policy_rev()}) -> {OUT.relative_to(ROOT)}")
    for k in ("allow", "flip", "reject"):
        summ(k, groups[k])
    print("  (shadow only: nothing here blocks a bet; a hard gate is an operator "
          "decision, see journal/proposals.md)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="pre-flight one candidate")
    c.add_argument("--est-prob", type=float, required=True)
    c.add_argument("--ask", type=float, required=True, help="best ask (the fill price)")
    c.add_argument("--bid", type=float, required=True, help="best bid")
    c.set_defaults(fn=cmd_check)
    l = sub.add_parser("ledger", help="rewrite strategy/policy-shadow.jsonl from the ledger")
    l.set_defaults(fn=cmd_ledger)
    args = ap.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
