#!/usr/bin/env python3
"""Find LLM API call sites in a codebase and flag likely token waste.

Heuristic, read-only, no network. It points you at code to read; it does not
prove anything on its own. Every finding should be confirmed by opening the file.

Recognises direct SDK calls (Anthropic, OpenAI, Gemini, LiteLLM, Vercel AI SDK)
and calls made through LangChain, Pydantic AI, LlamaIndex, CrewAI, DSPy, AutoGen,
Haystack, Semantic Kernel, the OpenAI Agents SDK, smolagents and Mastra.

Usage:
    python scan.py PATH [--json] [--include-tests]

Prints a readable summary, or JSON with --json. Test files are skipped unless
--include-tests is given.
"""
import argparse, io, json, os, re, sys, tokenize
from pathlib import Path

EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv", "venv", "env", "__pycache__",
             ".next", ".nuxt", "coverage", "site-packages", ".tox", ".mypy_cache"}
TEST_DIRS = {"tests", "test", "__tests__", "spec"}
TEST_FILE = re.compile(r"(^test_.*\.py$|_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$)")
MAX_FILE_BYTES = 1_500_000
WINDOW = 40  # lines after a call to look for its arguments

SDK_PATTERNS = {
    "anthropic": r"(\bimport anthropic\b|from anthropic\b|@anthropic-ai/sdk|new Anthropic\()",
    "openai": r"(\bimport openai\b|from openai\b|['\"]openai['\"]|new OpenAI\()",
    "google-genai": r"(google\.genai|google\.generativeai|@google/genai|@google/generative-ai)",
    "litellm": r"\blitellm\b",
    "langchain": r"(\bfrom langchain|\bimport langchain|@langchain/)",
    "pydantic-ai": r"(\bfrom pydantic_ai\b|\bimport pydantic_ai\b)",
    "vercel-ai": r"from ['\"]ai['\"]|@ai-sdk/",
    "llama-index": r"(\bfrom llama_index\b|\bimport llama_index\b)",
    "crewai": r"(\bfrom crewai\b|\bimport crewai\b)",
    "dspy": r"\bimport dspy\b|\bfrom dspy\b",
    "autogen": r"(\bfrom autogen|\bimport autogen|\bfrom ag2\b)",
    "haystack": r"(\bfrom haystack\b|\bimport haystack\b)",
    "semantic-kernel": r"(\bfrom semantic_kernel\b|\bimport semantic_kernel\b)",
    "openai-agents": r"(\bfrom agents import\b[^\n]*\bRunner\b|@openai/agents)",
    "smolagents": r"(\bfrom smolagents\b|\bimport smolagents\b)",
    "mastra": r"@mastra/",
}
# Direct SDK calls: matched in any file.
CALL_PATTERNS = [
    (r"\.messages\.create\(|\.messages\.stream\(", "anthropic messages"),
    (r"\.chat\.completions\.create\(|\.chat\.completions\.parse\(", "openai chat completions"),
    (r"\.responses\.create\(", "openai responses"),
    (r"\.generate_content\(|\.generateContent\(|\.models\.generate_content\(", "gemini generate_content"),
    (r"litellm\.a?completion\(", "litellm completion"),
    (r"\b(generateText|streamText|generateObject)\(", "vercel ai sdk"),
]
# Framework calls: only in files that import the framework, because the method names are generic.
FRAMEWORK_CALLS = [
    ("langchain", re.compile(r"\b([A-Za-z_][\w.]*)\.a?(invoke|stream|batch)\("), "langchain call"),
    ("pydantic-ai", re.compile(r"\b([A-Za-z_][\w.]*)\.(run|run_sync|run_stream|iter)\("), "pydantic-ai agent run"),
    ("pydantic-ai", re.compile(r"\b()(model_request|model_request_sync|model_request_stream)\("), "pydantic-ai direct request"),
    ("llama-index", re.compile(r"\b([A-Za-z_][\w.]*)\.(a?complete|a?chat|a?stream_complete|a?stream_chat|a?query|a?predict)\("), "llama-index call"),
    ("crewai", re.compile(r"\b([A-Za-z_][\w.]*)\.(kickoff|kickoff_async|kickoff_for_each|kickoff_for_each_async)\("), "crewai kickoff"),
    ("dspy", re.compile(r"\b()dspy\.(Predict|ChainOfThought|ReAct|ProgramOfThought|MultiChainComparison)\("), "dspy module"),
    ("autogen", re.compile(r"\b([A-Za-z_][\w.]*)\.(a?initiate_chat|run|run_stream|generate_reply)\("), "autogen agent run"),
    ("haystack", re.compile(r"\b([A-Za-z_][\w.]*)\.(run|run_async)\("), "haystack pipeline run"),
    ("semantic-kernel", re.compile(r"\b([A-Za-z_][\w.]*)\.(invoke|invoke_prompt|invoke_stream|get_chat_message_contents?)\("), "semantic-kernel call"),
    ("openai-agents", re.compile(r"\b()Runner\.(run|run_sync|run_streamed)\("), "openai agents run"),
    ("smolagents", re.compile(r"\b([A-Za-z_][\w.]*)\.run\("), "smolagents run"),
    ("mastra", re.compile(r"\b([A-Za-z_][\w.]*[Aa]gent)\.(generate|stream)\("), "mastra agent call"),
]
# Receivers that look like models always count; others that look like tools, retrievers, templates... don't.
MODEL_HINT = re.compile(r"llm|model|chat|agent|chain|runnable|claude|gpt|gemini", re.I)
# Receivers that are not model calls even in framework files (tools, retrievers, prompt templates...).
NOT_A_MODEL = re.compile(r"^self$|runner|tool|retriev|loader|vector|embed|splitter|parser|prompt|template|db|session|loop|asyncio|subprocess|executor|thread|pool|repl|server|app|browser|page|task", re.I)

MODEL_RE = re.compile(r"""model(?:_name)?\s*[=:]\s*['"]([A-Za-z0-9._:/@-]+)['"]""")
# Provider-prefixed model strings, as Pydantic AI and LangChain's init_chat_model take them.
MODEL_STR_RE = re.compile(r"""['"]((?:anthropic|openai(?:-chat|-responses)?|google(?:-gla|-vertex|-cloud)?|openrouter|bedrock|groq|mistral|deepseek|cerebras|vercel|azure):[A-Za-z0-9._:/@-]+)['"]""")
SLASH_MODEL_RE = re.compile(r"""['"]((?:anthropic|openai|gemini|vertex_ai|openrouter|bedrock|groq|deepseek|mistral|together_ai|azure)/[A-Za-z0-9._:/@-]+)['"]""")
BARE_MODEL_RE = re.compile(r"""['"]((?:claude|gpt|gemini|o[134]|deepseek|qwen|kimi|glm)[-.\w]*\d[-.\w]*)['"]""", re.I)
MAXTOK_RE = re.compile(r"max_tokens|max_output_tokens|max_completion_tokens|maxTokens|maxOutputTokens|max_new_tokens")
BASE_URL_RE = re.compile(r"""(base_url|baseURL|base_url\s*=|api_base)\s*[=:]\s*['"]([^'"]+)['"]""")
CACHE_RE = re.compile(r"cache_control|cachedContent|cached_content|prompt_cache|cacheControl|caches\.create|cachePoint|cache_point"
                      r"|anthropic_cache|openrouter_cache|bedrock_cache|cache_control_injection_points|enable_anthropic_prompt_caching|LITELLM_ENABLE_ANTHROPIC_PROMPT_CACHING|['\"]cache['\"]\s*\]?\s*[:=]\s*(True|['\"])|\bcache\s*=\s*True")
BATCH_RE = re.compile(r"batches\.create|/v1/batches|messages\.batches|batch_create|BatchPredictionJob|batches\.submit")
LOOP_RE = re.compile(r"^\s*(for\b|while\b|async for\b)|\.(map|forEach)\(|Promise\.all\(|kickoff_for_each|asyncio\.gather\(")
HISTORY_RE = re.compile(r"(messages|history|conversation|chat_history)\s*(\.append|\.push|\.extend|\+=|\.concat)")
VOLATILE_RE = re.compile(r"datetime\.now|datetime\.utcnow|time\.time\(\)|Date\.now\(|new Date\(|uuid|random\.|Math\.random")
SYSTEM_RE = re.compile(r"""\bsystem\s*[=:]|role['"]?\s*[:=]\s*['"]system['"]|system_instruction|systemInstruction|SystemMessage|instructions\s*=""")
BIG_LITERAL_RE = re.compile(r'("""|\'\'\'|`)(.{2500,}?)\1', re.S)
# Caching switched on behind a provider check: the classic way Claude-through-a-gateway loses its cache.
CACHE_GATE_RE = re.compile(r"""(==\s*['"]anthropic['"]|['"]anthropic['"]\s*==|isinstance\([^)]*Anthropic|startswith\(\s*['"]anthropic)""")
GATEWAY_RE = re.compile(r"openrouter|bedrock|vertex|azure|litellm", re.I)
CLAUDE_VIA_GATEWAY_RE = re.compile(r"""['"](?:openrouter[:/]anthropic/|bedrock[:/][^'"]*claude|vertex_ai/claude|(?:[a-z]{2}\.)?anthropic\.claude)""", re.I)
OPENROUTER_ID_RE = re.compile(r"""['"]anthropic/claude[^'"]*['"]""")

EXPENSIVE_HINTS = ["opus", "fable", "mythos", "astra", "neon", "gpt-6", "o3", "o4", "kimi-k3", "sonnet"]


def iter_files(root, include_tests=False):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                       and (include_tests or d not in TEST_DIRS)]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in EXTS and (include_tests or not TEST_FILE.search(fn)):
                try:
                    if p.stat().st_size <= MAX_FILE_BYTES:
                        yield p
                except OSError:
                    pass


JS_STR_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/|`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"", re.S)


def code_only(text, suffix, strings=True):
    """The file with strings and comments blanked out (line count kept), so docs that mention
    agent.run() or messages.create() aren't mistaken for calls."""
    if suffix == ".py":
        try:
            lines = text.splitlines(keepends=True)
            out = [list(l) for l in lines]
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT or (strings and (tok.type == tokenize.STRING or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1))):
                    (r0, c0), (r1, c1) = tok.start, tok.end
                    for r in range(r0, r1 + 1):
                        row = out[r - 1]
                        a = c0 if r == r0 else 0
                        b = c1 if r == r1 else len(row)
                        for k in range(a, min(b, len(row))):
                            if row[k] != "\n":
                                row[k] = " "
            return "".join("".join(r) for r in out).splitlines()
        except (tokenize.TokenError, IndentationError, SyntaxError, IndexError):
            pass
    blank = lambda m: m.group(0) if (not strings and m.group(0)[:1] in "'\"`") else re.sub(r"[^\n]", " ", m.group(0))
    return JS_STR_COMMENT.sub(blank, text).splitlines()


def find_model(lines, i):
    """The model string nearest a call: model=... after or before it, else a provider:model or bare model string."""
    after, before = "\n".join(lines[i:i + WINDOW]), "\n".join(lines[max(0, i - WINDOW):i])
    for rx in (MODEL_RE, MODEL_STR_RE, SLASH_MODEL_RE, BARE_MODEL_RE):
        m = rx.search(after) or rx.search(before)
        if m:
            return m.group(1)
    return None


def cache_gated(lines):
    """Line numbers where caching is switched on only inside a provider check for Anthropic."""
    hits = []
    for i, line in enumerate(lines):
        if CACHE_RE.search(line) and any(CACHE_GATE_RE.search(l) for l in lines[max(0, i - 8):i + 1]):
            hits.append(i + 1)
    return hits


def scan(root, include_tests=False):
    report = {"root": str(root), "files_scanned": 0, "sdks": {}, "calls": [], "repo": {}}
    any_cache = any_batch = False
    base_urls, gated, claude_gw = set(), [], []
    for path in iter_files(root, include_tests):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        report["files_scanned"] += 1
        rel = str(path.relative_to(root)) if Path(root).is_dir() else path.name
        sdks = [k for k, pat in SDK_PATTERNS.items() if re.search(pat, text)]
        for s in sdks:
            report["sdks"].setdefault(s, []).append(rel)
        file_cache = bool(CACHE_RE.search(text))
        file_batch = bool(BATCH_RE.search(text))
        any_cache |= file_cache
        any_batch |= file_batch
        for m in BASE_URL_RE.finditer(text):
            base_urls.add(m.group(2))
        lines = text.splitlines()
        code = code_only(text, path.suffix)
        g = cache_gated(lines)
        if g and GATEWAY_RE.search(text):
            gated.append({"file": rel, "lines": g})
        kept = "\n".join(code_only(text, path.suffix, strings=False))
        if CLAUDE_VIA_GATEWAY_RE.search(kept) or ("openrouter.ai" in kept and OPENROUTER_ID_RE.search(kept)):
            claude_gw.append({"file": rel, "cache_in_file": file_cache})
        file_history = bool(HISTORY_RE.search(text))
        file_volatile_system = bool(VOLATILE_RE.search(text)) and bool(SYSTEM_RE.search(text))
        file_maxtok = bool(MAXTOK_RE.search(text))
        big_literals = [len(m.group(2)) for m in BIG_LITERAL_RE.finditer(text)]

        def add_call(i, kind, framework):
            window = "\n".join(lines[i:i + WINDOW])
            model = find_model(lines, i)
            flags = []
            # Frameworks usually set the output limit where the model is built, not at the call.
            if not (file_maxtok if framework else MAXTOK_RE.search(window)):
                flags.append("no_output_limit")
            # Caching only pays off on a large, repeated prefix (system prompt, documents, chat history).
            repeated_context = bool(big_literals) or file_history or bool(SYSTEM_RE.search(window)) or framework
            if not file_cache and repeated_context:
                flags.append("no_cache_markers_in_file")
            if file_volatile_system:
                flags.append("possible_cache_breaker")
            if file_history:
                flags.append("growing_history")
            if any(LOOP_RE.search(l) for l in lines[max(0, i - 6):i + 1]):
                flags.append("call_in_loop")
            if big_literals:
                flags.append("large_static_prompt")
            if model and any(h in model.lower() for h in EXPENSIVE_HINTS):
                flags.append("frontier_model")
            if g:
                flags.append("cache_only_for_one_provider")
            report["calls"].append({"file": rel, "line": i + 1, "kind": kind, "model": model,
                                    "flags": flags, "big_literal_chars": max(big_literals) if big_literals else 0})

        for i, line in enumerate(code):
            for pat, kind in CALL_PATTERNS:
                if re.search(pat, line):
                    add_call(i, kind, False)
            for sdk, rx, kind in FRAMEWORK_CALLS:
                if sdk not in sdks:
                    continue
                m = rx.search(line)
                recv = m.group(1) if m else ""
                if m and (not recv or MODEL_HINT.search(recv) or not NOT_A_MODEL.search(recv)):
                    add_call(i, kind, True)
    report["repo"] = {"uses_cache_markers": any_cache, "uses_batch_api": any_batch,
                      "base_urls": sorted(base_urls), "cache_only_for_one_provider": gated,
                      "claude_via_gateway": claude_gw}
    return report


FLAG_TEXT = {
    "no_output_limit": "no max_tokens-style limit near the call (or, for framework calls, anywhere in the file) (technique 3: shorter outputs)",
    "no_cache_markers_in_file": "no explicit caching in this file (technique 1: caching; OpenAI and Gemini cache automatically, Claude only when asked)",
    "possible_cache_breaker": "file builds a system prompt and also uses time/uuid/random values (check they aren't in the prompt prefix)",
    "growing_history": "conversation list is appended to (technique 5: trim context)",
    "large_static_prompt": "large string literal in file, a strong caching candidate",
    "call_in_loop": "API called once per item in a loop; if nobody waits for the result, a batch candidate (technique 4)",
    "frontier_model": "expensive model; check whether every request needs it (technique 2: routing)",
    "cache_only_for_one_provider": "caching is switched on only inside an Anthropic check; Claude via OpenRouter, Bedrock or Vertex may never cache (technique 1)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-tests", action="store_true", help="also scan test files")
    a = ap.parse_args()
    if not os.path.exists(a.path):
        sys.exit(f"Path not found: {a.path}")
    r = scan(a.path, a.include_tests)
    if a.json:
        print(json.dumps(r, indent=1))
        return
    print(f"Scanned {r['files_scanned']} files under {r['root']}{'' if a.include_tests else ' (tests skipped)'}")
    print("SDKs: " + (", ".join(f"{k} ({len(v)} files)" for k, v in r["sdks"].items()) or "none found"))
    print(f"Explicit caching anywhere: {'yes' if r['repo']['uses_cache_markers'] else 'no'}")
    print(f"Batch API anywhere: {'yes' if r['repo']['uses_batch_api'] else 'no'}")
    print("Base URLs: " + (", ".join(r["repo"]["base_urls"]) or "SDK defaults (direct to the model maker)"))
    for g in r["repo"]["cache_only_for_one_provider"]:
        print(f"! {g['file']}: caching switched on only for direct Anthropic (line {', '.join(map(str, g['lines']))}); "
              "check every route to Claude (OpenRouter, Bedrock, Vertex) gets it too")
    for c in r["repo"]["claude_via_gateway"]:
        if not c["cache_in_file"]:
            print(f"! {c['file']}: Claude through a gateway with no cache setting in the file; on OpenRouter and Bedrock Claude only caches when asked")
    print(f"\n{len(r['calls'])} LLM call sites:")
    for c in r["calls"]:
        print(f"\n- {c['file']}:{c['line']} [{c['kind']}] model={c['model'] or 'not found nearby'}")
        for f in c["flags"]:
            print(f"    · {FLAG_TEXT[f]}")
    if not r["calls"]:
        print("No call sites found. The project may call LLMs through another framework, an HTTP client or another language; "
              "search for the provider's URL or SDK name.")


if __name__ == "__main__":
    main()
