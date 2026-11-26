# llm-cost-audit

An AI agent skill that audits your code's LLM API calls for wasted tokens and tells you, in dollars, what each fix would save.

Point Claude (or any agent that supports Agent Skills) at a project and ask *"why is our Claude bill so high?"* or *"audit our LLM costs"*. The skill:

1. **Scans** the codebase for Anthropic, OpenAI, Gemini, LiteLLM and Vercel AI SDK calls, and for calls made through LangChain, Pydantic AI, LlamaIndex, CrewAI, DSPy, AutoGen, Haystack, Semantic Kernel, the OpenAI Agents SDK, smolagents and Mastra.
2. **Flags** likely waste: no caching, cache-breaking timestamps in prompts, unbounded chat history, frontier models on simple tasks, missing output limits, per-item loops that could be batch jobs.
3. **Estimates** monthly savings per fix from real per-token prices.
4. **Writes a report** ordered by dollars saved, with before/after code, and applies the fixes if you ask.

It covers the six techniques from [How to cut your LLM API bill](https://lesspertoken.com/blog/token-saving-basics/): caching, routing to a smaller model, shorter outputs, batch APIs, trimming context, and where you buy the tokens.

## Install

**Claude Code:** copy the `llm-cost-audit` folder into `~/.claude/skills/` (for all your projects) or `.claude/skills/` inside one project.

**Claude apps:** download `llm-cost-audit.skill` from Releases and upload it under Skills in Claude's settings.

**Other agents:** any agent that reads `SKILL.md` skills can use the folder as is. The scripts need Python 3.8+ and no packages.

## Example

Run on a small project with a coding agent and a ticket tagger:

```
$ python scripts/scan.py ./my-app
2 LLM call sites:

- src/agent.py:8  [anthropic messages]  model=claude-sonnet-5-5
    · no explicit caching in this file
    · file builds a system prompt and also uses time/uuid/random values
    · conversation list is appended to
    · expensive model; check whether every request needs it

- src/tagger.ts:6  [openai chat completions]  model=gpt-6-2-astra
    · no max_tokens-style limit near the call
    · API called once per item in a loop; a batch candidate
    · expensive model; check whether every request needs it
```

```
$ python scripts/estimate.py --model claude-sonnet-5-5 --input 50 --output 2 --cache 0.85 --shorter 0.3 --to saygm
Claude Sonnet 5.5, 50M input + 2M output tokens/month (prices checked 2026-11-26)
  Bill today                           $    120.00
  Buy via saygm                        $     61.80  (-$58.20)
  Trim context                         $     61.80  (+$0.00)
  Shorter outputs                      $     58.71  (-$3.09)
  Route easy work to Claude Haiku 5.5  $     58.71  (+$0.00)
  Caching                              $     17.12  (-$41.59)
  Batch jobs                           $     17.12  (+$0.00)
Saving: $102.88/month (86%), $1,234.51/year
```

The agent then confirms each flag by reading the code (here: `datetime.now()` at the top of the system prompt breaks caching on every call; the ticket classifier runs GPT-6.2 Astra on a three-way label) and writes the report with fixes.

## Prices

`llm-cost-audit/references/prices.json` is a snapshot of per-token prices for 14 popular models: list price, OpenRouter's listed price (plus its 5.5% card fee) and SayGM's best listed price. Gateway prices change often, and on some open models OpenRouter is currently cheaper; the estimator says so when that's the case. The snapshot date is inside the file and printed with every estimate. Current prices are on [lesspertoken.com](https://lesspertoken.com/).

## Changes

**1.3 (26 November 2026)**
- The scanner also recognises LlamaIndex, CrewAI (`kickoff`, `kickoff_for_each` as a batch candidate), DSPy, AutoGen/AG2, Haystack, Semantic Kernel, the OpenAI Agents SDK (`Runner.run`), smolagents and Mastra, and LiteLLM-style model strings like `'anthropic/claude-opus-5-5'`.
- The Claude-through-a-gateway warning now fires only for real gateway routes (OpenRouter, Bedrock, Vertex). In LiteLLM-based frameworks, `anthropic/claude-...` means direct Anthropic.
- LiteLLM's `cache_control_injection_points` and its global Anthropic prompt-caching switch count as caching.

Tested on a project per framework, the official CrewAI examples (16 call sites, all real) and the two audited projects (unchanged).

**1.2 (26 November 2026)**
- The scanner recognises calls made through LangChain (`invoke`, `ainvoke`, `stream`, `batch`) and Pydantic AI (`Agent.run`, `run_sync`, `run_stream`, `iter`, `model_request`), and provider-prefixed model strings like `'openrouter:anthropic/claude-sonnet-5.5'`.
- New warnings: caching switched on only for direct Anthropic, and Claude reached through OpenRouter or Bedrock with no cache setting.
- Strings, comments and test files no longer count as call sites.
- Price snapshot refreshed on 26 November 2026.

Tested on [browser-use](https://lesspertoken.com/blog/browser-use-cost-audit/) (0 call sites found before, 5 now, and a warning on the exact line of the caching gap we reported) and [GPT Researcher](https://lesspertoken.com/blog/gpt-researcher-cost-audit/) (1 before, 11 now).

**1.1:** caching is only suggested where a call has repeated context (system prompt, large static prompt or growing history).

**1.0:** first release.

## Disclosure

This skill is published by [LessPerToken](https://lesspertoken.com/), which compares AI API prices and earns a reward when people sign up to SayGM through its links. The skill reports gateway prices neutrally next to list prices, and recommends direct or sealed routes for secrets and client data. Every other technique saves money wherever you buy your tokens.

## License

MIT
