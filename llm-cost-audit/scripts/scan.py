#!/usr/bin/env python3
"""Find LLM API call sites in a codebase and flag likely token waste.

Heuristic, read-only, no network. It points you at code to read; it does not
prove anything on its own. Every finding should be confirmed by opening the file.

Usage:
    python scan.py PATH [--json]

Prints a readable summary, or JSON with --json.
"""
import argparse, json, os, re, sys
from pathlib import Path

EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv", "venv", "env", "__pycache__",
             ".next", ".nuxt", "coverage", "site-packages", ".tox", ".mypy_cache"}
MAX_FILE_BYTES = 1_500_000
WINDOW = 40  # lines after a call to look for its arguments

SDK_PATTERNS = {
    "anthropic": r"(\bimport anthropic\b|from anthropic\b|@anthropic-ai/sdk|new Anthropic\()",
    "openai": r"(\bimport openai\b|from openai\b|['\"]openai['\"]|new OpenAI\()",
    "google-genai": r"(google\.genai|google\.generativeai|@google/genai|@google/generative-ai)",
    "litellm": r"\blitellm\b",
    "langchain": r"\blangchain",
    "vercel-ai": r"from ['\"]ai['\"]|@ai-sdk/",
}
CALL_PATTERNS = [
    (r"\.messages\.create\(|\.messages\.stream\(", "anthropic messages"),
    (r"\.chat\.completions\.create\(|\.chat\.completions\.parse\(", "openai chat completions"),
    (r"\.responses\.create\(", "openai responses"),
    (r"\.generate_content\(|\.generateContent\(|\.models\.generate_content\(", "gemini generate_content"),
    (r"litellm\.a?completion\(", "litellm completion"),
    (r"\b(generateText|streamText|generateObject)\(", "vercel ai sdk"),
]
MODEL_RE = re.compile(r"""model\s*[=:]\s*['"]([A-Za-z0-9._:/@-]+)['"]""")
MAXTOK_RE = re.compile(r"max_tokens|max_output_tokens|max_completion_tokens|maxTokens|maxOutputTokens")
BASE_URL_RE = re.compile(r"""(base_url|baseURL|base_url\s*=|api_base)\s*[=:]\s*['"]([^'"]+)['"]""")
CACHE_RE = re.compile(r"cache_control|cachedContent|cached_content|prompt_cache|cacheControl|caches\.create")
BATCH_RE = re.compile(r"batches\.create|/v1/batches|messages\.batches|batch_create|BatchPredictionJob|batches\.submit")
LOOP_RE = re.compile(r"^\s*(for\b|while\b|async for\b)|\.(map|forEach)\(|Promise\.all\(")
HISTORY_RE = re.compile(r"(messages|history|conversation|chat_history)\s*(\.append|\.push|\.extend|\+=|\.concat)")
VOLATILE_RE = re.compile(r"datetime\.now|datetime\.utcnow|time\.time\(\)|Date\.now\(|new Date\(|uuid|random\.|Math\.random")
SYSTEM_RE = re.compile(r"""\bsystem\s*[=:]|role['"]?\s*[:=]\s*['"]system['"]|system_instruction|systemInstruction""")
BIG_LITERAL_RE = re.compile(r'("""|\'\'\'|`)(.{2500,}?)\1', re.S)

EXPENSIVE_HINTS = ["opus", "fable", "mythos", "astra", "neon", "gpt-6", "o3", "o4", "kimi-k3", "sonnet"]


def iter_files(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in EXTS:
                try:
                    if p.stat().st_size <= MAX_FILE_BYTES:
                        yield p
                except OSError:
                    pass


def scan(root):
    report = {"root": str(root), "files_scanned": 0, "sdks": {}, "calls": [], "repo": {}}
    any_cache = any_batch = False
    base_urls = set()
    for path in iter_files(root):
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
        file_history = bool(HISTORY_RE.search(text))
        file_volatile_system = bool(VOLATILE_RE.search(text)) and bool(SYSTEM_RE.search(text))
        big_literals = [len(m.group(2)) for m in BIG_LITERAL_RE.finditer(text)]
        for i, line in enumerate(lines):
            for pat, kind in CALL_PATTERNS:
                if not re.search(pat, line):
                    continue
                window = "\n".join(lines[i:i + WINDOW])
                before = "\n".join(lines[max(0, i - WINDOW):i])
                mm = MODEL_RE.search(window) or MODEL_RE.search(before)
                model = mm.group(1) if mm else None
                flags = []
                if not MAXTOK_RE.search(window):
                    flags.append("no_output_limit")
                if not file_cache:
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
                report["calls"].append({"file": rel, "line": i + 1, "kind": kind, "model": model,
                                        "flags": flags, "big_literal_chars": max(big_literals) if big_literals else 0})
    report["repo"] = {"uses_cache_markers": any_cache, "uses_batch_api": any_batch,
                      "base_urls": sorted(base_urls)}
    return report


FLAG_TEXT = {
    "no_output_limit": "no max_tokens-style limit near the call (technique 3: shorter outputs)",
    "no_cache_markers_in_file": "no explicit caching in this file (technique 1: caching; some providers cache automatically)",
    "possible_cache_breaker": "file builds a system prompt and also uses time/uuid/random values (check they aren't in the prompt prefix)",
    "growing_history": "conversation list is appended to (technique 5: trim context)",
    "large_static_prompt": "large string literal in file, a strong caching candidate",
    "call_in_loop": "API called once per item in a loop; if nobody waits for the result, a batch candidate (technique 4)",
    "frontier_model": "expensive model; check whether every request needs it (technique 2: routing)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.path):
        sys.exit(f"Path not found: {a.path}")
    r = scan(a.path)
    if a.json:
        print(json.dumps(r, indent=1))
        return
    print(f"Scanned {r['files_scanned']} files under {r['root']}")
    print("SDKs: " + (", ".join(f"{k} ({len(v)} files)" for k, v in r["sdks"].items()) or "none found"))
    print(f"Explicit caching anywhere: {'yes' if r['repo']['uses_cache_markers'] else 'no'}")
    print(f"Batch API anywhere: {'yes' if r['repo']['uses_batch_api'] else 'no'}")
    print("Base URLs: " + (", ".join(r["repo"]["base_urls"]) or "SDK defaults (direct to the model maker)"))
    print(f"\n{len(r['calls'])} LLM call sites:")
    for c in r["calls"]:
        print(f"\n- {c['file']}:{c['line']}  [{c['kind']}]  model={c['model'] or 'not found nearby'}")
        for f in c["flags"]:
            print(f"    · {FLAG_TEXT[f]}")
    if not r["calls"]:
        print("No call sites found. The project may call LLMs through a wrapper, an HTTP client or another language; search for the provider's URL or SDK name.")


if __name__ == "__main__":
    main()
