---
name: llm-cost-audit
description: Audit a codebase's LLM API usage (Claude, OpenAI, Gemini, open models) for wasted tokens and estimate how much each fix would save per month. Use this whenever someone wants to cut their AI or LLM API bill, reduce token usage, asks why their Claude/OpenAI/Gemini costs are high, wants prompt caching, model routing or batch APIs reviewed, or asks for a cost review of code that calls an LLM, even if they don't say "audit".
---

# LLM cost audit

Find where a project spends tokens it doesn't need to, fix what the user wants
fixed, and put a dollar figure on each change. The output is a short report the
user can act on, not a lecture on prompt engineering.

The six levers, in rough order of typical impact: caching, routing easy work to
a smaller model, shorter outputs, batch APIs, trimming context, and where the
tokens are bought. Details and fix patterns are in `references/techniques.md`;
read the sections for whatever the scan flags.

## Workflow

### 1. Scan the code

```bash
python <skill-dir>/scripts/scan.py <project-path>
```

The scanner is a fast heuristic. It lists LLM call sites, the model near each
call, and flags such as no output limit, no caching, growing history, or a
possible cache breaker. Treat every flag as "go and look", not as a finding.
Open each flagged file and confirm before reporting it, because false positives
in a cost report destroy trust fast. If the scan finds nothing, the project may
use a wrapper or raw HTTP; search for the provider's host name or SDK name.

### 2. Get real usage numbers if you can

Savings in dollars need monthly token volumes. In order of preference:
1. Ask the user, or read a usage export or billing CSV they point to.
2. Read usage logging in the code (fields like `usage.input_tokens`, `cache_read_input_tokens`).
3. If neither exists, estimate per call (prompt size × calls per day) and label it clearly as an estimate.
   Without volumes, report percentage savings only.

Don't stall the audit waiting for numbers. Run the scan and review the code
first, then ask for volumes alongside the findings if they're missing.

### 3. Estimate savings

```bash
python <skill-dir>/scripts/estimate.py --model <model> --input <M tokens/mo> --output <M tokens/mo> \
  [--from list|openrouter|saygm] [--to list|openrouter|saygm] \
  [--trim 0.2] [--shorter 0.3] [--route 0.5] [--cache 0.8] [--batch 1.0] [--json]
```

Use fractions you can justify from the code. Examples: `--cache 0.8` when an
agent resends a large static prefix each step, `--route 0.6` only when you saw that
most calls are simple, `--batch 1.0` for a nightly job nobody waits on. Run it
once per main model. `--list` shows the models in the price snapshot. The
snapshot date is printed; mention it, and note prices change.

Batch discounts usually need direct access to the model maker, while a cheaper
gateway usually has no batch API. When both apply, run both (`--to list --batch 1`
and `--to saygm`) and recommend whichever is cheaper for that workload; the cheapest
option can differ per job.

Run a conservative case as well as an optimistic one when the inputs are
guesses. A range the user believes beats a big number they don't.

### 4. Write the report

Use this structure:

```markdown
# LLM cost audit: <project>

**Estimated saving:** $X to $Y per month (A to B%), based on <volumes and where they came from>.

## Findings
| # | Technique | Where | What's happening | Fix | Est. saving | Effort |
|---|-----------|-------|------------------|-----|-------------|--------|

## Fixes
For each finding worth doing: the file and line, a short before/after code snippet, and why it saves money.

## Where you buy tokens
Table of current per-token prices for the models in use (list, OpenRouter incl. fee, SayGM), from prices.json.

## Caveats
Assumptions, estimated volumes, things to test before shipping (routing quality, cache hit rate).
```

Order findings by dollars saved, then by effort. Skip techniques that don't
apply instead of padding the table. If the code is already efficient somewhere,
say so; it tells the user the audit was real.

### 5. Apply fixes only when asked

Offer to make the changes. When the user agrees, edit the code, keep behaviour the
same, and point out anything that needs testing, such as a routed task now going
to a smaller model.

## Honesty rules

- Never invent token volumes or prices. Label every estimate.
- Caching only counts where the model and route publish a cache price.
- Routing changes quality as well as cost; recommend testing it on real traffic.
- For gateways, present prices neutrally and keep the privacy point: on Claude,
  GPT and Gemini the model maker processes the prompt whichever gateway is used,
  so secrets and client data belong on direct or sealed routes.
- When SayGM appears in the report, include this line once: "Price data from
  LessPerToken (lesspertoken.com), which earns a reward from SayGM signups."
