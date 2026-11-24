#!/usr/bin/env python3
"""Estimate a monthly LLM bill and the saving from each token-saving technique.

Uses the price snapshot in ../references/prices.json (USD per 1M tokens).
Steps are applied in order, each on top of the previous one, so they add up
to the new bill: gateway, trim context, shorter outputs, routing, caching, batch.

Usage examples:
    python estimate.py --list
    python estimate.py --model claude-sonnet-5-5 --input 50 --output 2 --cache 0.85
    python estimate.py --model "Gemini 4 Neon" --input 20 --output 5 --to list --batch 1 --json

--input / --output are millions of tokens per month.
--from / --to are where tokens are bought: list (model maker), openrouter, saygm.
Fractions (0 to 1): --trim (fewer input tokens), --shorter (fewer output tokens),
--route (share of requests sent to the family's cheaper model), --cache (share of
input served from cache), --batch (share of workload run through a batch API).
"""
import argparse, json, re, sys
from pathlib import Path

PRICES = json.loads((Path(__file__).resolve().parent.parent / "references" / "prices.json").read_text())
FEE = PRICES["openrouter_card_fee"]
MODELS = PRICES["models"]


def norm(s):
    s = s.lower().split("/")[-1]
    s = re.sub(r"[._ ]", "-", s)
    s = re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2}|latest|preview)$", "", s)
    return s


def find_model(q):
    n = norm(q)
    for m in MODELS:
        if n in (m["slug"], norm(m["name"])):
            return m
    hits = [m for m in MODELS if n.startswith(m["slug"]) or m["slug"].startswith(n)]
    if len(hits) == 1:
        return hits[0]
    hits = [m for m in MODELS if all(t in m["slug"] for t in n.split("-") if t)]
    return hits[0] if len(hits) == 1 else None


def blend(m):
    return 5 * m["list"]["in"] + m["list"]["out"]


def small_for(m):
    c = [x for x in MODELS if x["maker"] == m["maker"] and x["slug"] != m["slug"] and blend(x) < blend(m) * 0.6]
    return min(c, key=blend) if c else None


def price(m, route):
    key = {"list": "list", "openrouter": "openrouter", "saygm": "saygm"}[route]
    p, f = m[key], (1 + FEE if route == "openrouter" else 1)
    return {"in": p["in"] * f, "out": p["out"] * f, "cache": None if p.get("cache") is None else p["cache"] * f}


def bill(m, route, I, O, trim=0, shorter=0, route_share=0, cache=0, batch=0):
    sm = small_for(m)
    i, o = I * (1 - trim), O * (1 - shorter)
    s = route_share if sm else 0
    total = 0.0
    for mod, share in ((m, 1 - s), (sm, s)):
        if mod is None or share <= 0:
            continue
        p = price(mod, route)
        c = cache if p["cache"] is not None else 0
        total += i * share * ((1 - c) * p["in"] + c * (p["cache"] or 0)) + o * share * p["out"]
    b = batch if route == "list" else 0
    return total * (1 - 0.5 * b)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list known models and exit")
    ap.add_argument("--model")
    ap.add_argument("--input", type=float, default=0)
    ap.add_argument("--output", type=float, default=0)
    ap.add_argument("--from", dest="frm", default="list", choices=["list", "openrouter", "saygm"])
    ap.add_argument("--to", default="list", choices=["list", "openrouter", "saygm"])
    for k in ("trim", "shorter", "route", "cache", "batch"):
        ap.add_argument("--" + k, type=float, default=0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.list:
        print(f"Prices checked {PRICES['checked']} ({PRICES['source']})")
        for m in MODELS:
            l = m["list"]
            print(f"  {m['slug']:<28} {m['name']:<26} list ${l['in']}/{l['out']}  cache {l.get('cache')}")
        return
    if not a.model:
        sys.exit("--model is required (use --list to see options)")
    m = find_model(a.model)
    if not m:
        sys.exit(f"Unknown model '{a.model}'. Use --list. If it isn't listed, estimate with the closest model and say so.")
    for k in ("trim", "shorter", "route", "cache", "batch"):
        v = getattr(a, k)
        if not 0 <= v <= 1:
            sys.exit(f"--{k} must be a fraction between 0 and 1")

    sm = small_for(m)
    to_p = price(m, a.to)
    notes = []
    if a.route and not sm:
        notes.append(f"No cheaper {m['maker']} model in the price list, so routing is not counted.")
    if a.cache and to_p["cache"] is None:
        notes.append(f"No cache price published for {m['name']} on '{a.to}', so caching is not counted.")
    if a.batch and a.to != "list":
        notes.append("Batch discount counted only when buying direct from the model maker ('--to list').")
    if m["maker"] == "Open models" and "list" in (a.frm, a.to):
        notes.append("Open-model 'list' prices are a reference price; there is no single maker API.")

    I, O = a.input, a.output
    seq = [
        ("Bill today", bill(m, a.frm, I, O)),
        (f"Buy via {a.to}" if a.to != a.frm else "Same place to buy", bill(m, a.to, I, O)),
        ("Trim context", bill(m, a.to, I, O, a.trim)),
        ("Shorter outputs", bill(m, a.to, I, O, a.trim, a.shorter)),
        (f"Route easy work to {sm['name']}" if sm else "Routing", bill(m, a.to, I, O, a.trim, a.shorter, a.route)),
        ("Caching", bill(m, a.to, I, O, a.trim, a.shorter, a.route, a.cache)),
        ("Batch jobs", bill(m, a.to, I, O, a.trim, a.shorter, a.route, a.cache, a.batch)),
    ]
    steps = [{"step": s, "monthly": round(v, 2), "change": round(v - seq[k - 1][1], 2) if k else 0.0}
             for k, (s, v) in enumerate(seq)]
    now, new = seq[0][1], seq[-1][1]
    res = {"model": m["name"], "small_model": sm["name"] if sm else None, "prices_checked": PRICES["checked"],
           "monthly_now": round(now, 2), "monthly_new": round(new, 2), "monthly_saving": round(now - new, 2),
           "saving_pct": round((1 - new / now) * 100) if now else 0, "yearly_saving": round((now - new) * 12, 2),
           "steps": steps, "notes": notes}
    if a.json:
        print(json.dumps(res, indent=1))
        return
    print(f"{m['name']}, {I:g}M input + {O:g}M output tokens/month (prices checked {PRICES['checked']})")
    for s in steps:
        ch = "" if s["step"] == "Bill today" else f"  ({'-' if s['change'] < 0 else '+'}${abs(s['change']):,.2f})"
        print(f"  {s['step']:<36} ${s['monthly']:>10,.2f}{ch}")
    if res["monthly_saving"] >= 0:
        print(f"Saving: ${res['monthly_saving']:,.2f}/month ({res['saving_pct']}%), ${res['yearly_saving']:,.2f}/year")
    else:
        print(f"This would COST MORE: +${-res['monthly_saving']:,.2f}/month (+{-res['saving_pct']}%). Don't recommend this change.")
    for n in notes:
        print("Note: " + n)


if __name__ == "__main__":
    main()
