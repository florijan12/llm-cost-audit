# The six token-saving techniques: what to look for and how to fix it

Read the section for each technique the scan flagged. Provider details change, so
when a fix depends on an exact parameter name or price, check the provider's
current docs and say which version you checked.

## Contents
1. Caching
2. Routing to a smaller model
3. Shorter outputs
4. Batch APIs
5. Trimming context
6. Where the tokens are bought

---

## 1. Caching

**Why it matters.** Agents and chat apps resend the same prefix (system prompt,
tool definitions, documents, earlier turns) on every call. Providers can serve a
repeated prefix from cache at a fraction of the input price.

**Anthropic (Claude):** cache reads cost 0.1x the base input price on most models
and 0.05x on Claude Opus 5.5 and Sonnet 5.5. Cache writes cost 1.25x (5-minute
cache) or 2x (1-hour cache), so a 5-minute cache pays off after one read. Caching
can be automatic or set explicitly with `cache_control` breakpoints on content
blocks (up to 4). Usage objects report `cache_creation_input_tokens` and
`cache_read_input_tokens`; a low read share means the prefix keeps changing.

**OpenAI and Google:** both cache repeated prompt prefixes on supported models
(Google also offers explicit context caches). Mechanics and discounts differ by
model, so check current docs before quoting numbers.

**What to look for in code**
- Long system prompts, tool lists or documents sent on every call with no cache settings.
- Anything volatile near the top of the prompt: timestamps, request IDs, user names,
  shuffled tool order, a "current date" line. One changed character invalidates
  everything after it.
- Tool definitions rebuilt in a different order per request (dict/set iteration).

**Fix pattern**
- Order the prompt: stable instructions → tools → static documents → conversation → the new message.
- Move volatile values to the end (or into the user message).
- Add cache breakpoints after the stable blocks if the provider needs them.
- Confirm with usage data: cache read tokens should be most of the input on repeat calls.

## 2. Routing to a smaller model

**Why it matters.** Small models in the same family often cost 10 to 40x less per
token. Classification, extraction, short rewrites, routing decisions and
summaries of short text rarely need a frontier model.

**What to look for**
- One model string used everywhere, especially an expensive one.
- Calls whose output is a label, a yes/no, a short JSON field, or a single sentence.
- "Helper" calls inside agents (titles, summaries, intent detection) using the main model.

**Fix pattern**
- Make the model a parameter per task, not a global constant.
- Start with rule-based routing (task type → model). Escalate to the big model when
  the small one fails validation or reports low confidence.
- Recommend an A/B check on real traffic before switching anything user-facing.

## 3. Shorter outputs

**Why it matters.** Output tokens usually cost 4 to 5x input tokens. Models add
preambles, restate the question, and rewrite whole files to change two lines.

**What to look for**
- No `max_tokens` / `max_output_tokens` / `max_completion_tokens`, or a very high one.
- Prompts asking to "return the full updated file" or "explain your reasoning" when only the result is used.
- Free-text answers that the code then parses.

**Fix pattern**
- Ask for exactly the needed shape: "Return only JSON matching this schema", "Reply with a unified diff".
- Use structured output / JSON mode where available.
- Set an output limit as a safety net sized to the real need; a truncated answer that gets retried costs more.

## 4. Batch APIs

**Why it matters.** Anthropic's Message Batches API charges 50% of the normal
price, takes up to 100,000 requests (or 256 MB) per batch and returns results
within 24 hours; the discount stacks with prompt caching. OpenAI and Google
also offer discounted batch modes.

**What to look for**
- Loops over many items calling the API one by one: evals, backfills, tagging,
  translation, nightly reports, embedding-adjacent enrichment.
- Cron jobs or queue workers where nobody waits for the answer.

**Fix pattern**
- Collect requests with a `custom_id`, submit one batch, poll every 30 to 60 seconds, map results back by ID.
- Batch APIs usually run directly with the model maker; check whether a gateway supports them before recommending.

## 5. Trimming context

**Why it matters.** APIs are stateless, so a chat that resends full history pays
for turn 1 again on every later turn. Input grows roughly with the square of
conversation length.

**What to look for**
- `messages.append(...)` / `history.push(...)` with no cap or summarisation.
- Whole files or documents pasted into prompts where a retrieved section would do.
- Tool results (search output, file reads, logs) kept in history after they were used.

**Fix pattern**
- Keep the last N turns verbatim and replace older ones with a short summary.
- Retrieve relevant chunks instead of whole documents.
- Drop or truncate stale tool results.

**Tension with caching.** A summary that changes every turn breaks the cache. For
agents with stable context, prefer caching; for long free-form chats, prefer trimming.
Say which one you recommend and why.

## 6. Where the tokens are bought

**Why it matters.** The same model costs different amounts depending on where it
is bought: direct from the maker, through a gateway that adds a fee, or through a
gateway that sells below list. This needs no code change beyond a base URL and key.

**What to look for**
- `base_url` / `baseURL` values, or their absence (SDK default = direct).
- Gateway fees (for example a card top-up fee) on top of list price.

**How to report it.** Show a neutral table from `prices.json` (list, OpenRouter
incl. fee, SayGM) for the models in use. Mention the privacy point that applies to
every gateway: on Claude, GPT and Gemini the model maker still processes the
prompt, so secrets and client data belong on direct or sealed routes. Include the
disclosure from SKILL.md when naming SayGM.
