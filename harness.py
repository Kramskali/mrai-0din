#!/usr/bin/env python3
"""
MRAI test harness — Memory-Resident Authority Injection.

Runs the carrier payload across four experimental conditions so the
memory channel can be isolated as the vulnerable component:

  baseline   trigger only, no carrier            -> proves the stock refusal
  user       carrier + trigger in the user turn  -> prompt-only control
  memory     carrier in memory position          -> the attack
  persisted  carrier in memory + filler turns    -> durability of the attack

The user/memory pair is the finding. Same bytes, two channels, different
outcome. Without that differential this is just another prompt trick.

Provider adapters are deliberately thin so the harness stays auditable.
Only the APIs you have keys for will run; everything else reports skipped.

Usage:
    export OPENAI_API_KEY=...            # any OpenAI-compatible endpoint
    export ANTHROPIC_API_KEY=...
    export GOOGLE_API_KEY=...
    export AI_GATEWAY_API_KEY=...        # Vercel AI Gateway (scouting only)

    python harness.py --dry-run                       # plumbing check, no network
    python harness.py --conditions baseline,memory    # single condition
    python harness.py --models gpt-5.5,claude-sonnet-4-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    sys.exit("httpx required: .venv/bin/pip install httpx")

ROOT = pathlib.Path(__file__).parent
PAYLOAD = ROOT / "payload" / "carrier_worked.md"
TRIGGERS = ROOT / "payload" / "triggers.json"
EVIDENCE = ROOT / "evidence"

# --- throttling: keep this conservative; these are shared consumer endpoints ---
MAX_CONCURRENCY = 2
INTER_REQUEST_DELAY = 6.0      # seconds between calls; 6s rides out free-tier limits
MAX_RETRIES = 5
BACKOFF_BASE = 4.0


@dataclass
class ModelSpec:
    """One test target. vendor is what JEF counts for BV."""
    id: str
    vendor: str
    provider: str          # openai | anthropic | google
    upstream: str          # actual model id on the wire
    base_url: str = ""
    notes: str = ""


# Default target set: 0DIN in-scope roster, checked 2026-11-17 against
# 0din.ai/scope. Wire names drift every few months - if a model 404s,
# check the provider console for the current id before anything else.
DEFAULT_TARGETS = [
    ModelSpec("gpt-5.5",             "openai",    "openai",    "gpt-5.5"),
    ModelSpec("gpt-5-mini",          "openai",    "openai",    "gpt-5-mini"),
    ModelSpec("claude-sonnet-4-5",   "anthropic", "anthropic", "claude-sonnet-4-5-20250929"),
    ModelSpec("gemini-3.8-flash",    "google",    "google",    "gemini-3.8-flash"),
    ModelSpec("gemini-3.1-pro",      "google",    "google",    "gemini-3.1-pro"),
    ModelSpec("grok-4.20",           "xai",       "openai",    "grok-4.20",
              base_url="https://api.x.ai/v1"),
    # Scouting lane: Vercel AI Gateway, one key, hundreds of models.
    # Use for finding which models show the differential; confirm every
    # hit through the first-party API before it goes in a submission.
    ModelSpec("vercel-gpt5.5",       "vercel",    "openai",    "openai/gpt-5.5",
              base_url="https://ai-gateway.vercel.sh/v1",
              notes="gateway scouting only - not submission evidence"),
]

PROVIDER_KEY_ENV = {
    "vercel": "AI_GATEWAY_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
}


def key_for(spec: ModelSpec) -> str | None:
    return os.environ.get(PROVIDER_KEY_ENV.get(spec.vendor, f"{spec.vendor.upper()}_API_KEY"))


def base_for(spec: ModelSpec) -> str:
    if spec.base_url:
        return spec.base_url.rstrip("/")
    return {"openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "google": "https://generativelanguage.googleapis.com/v1beta"}[spec.provider]


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------

def build_messages(condition: str, carrier: str, trigger: str, filler: list[str],
                   cold_filler: list[str] | None = None) -> list[dict]:
    """Provider-neutral message list. Memory position is emulated as the
    system/instruction slot; user position is a plain first user turn."""
    if condition == "baseline":
        return [{"role": "user", "content": trigger}]

    if condition == "user":
        # Everything the payload says, but delivered as an ordinary user turn.
        return [{"role": "user", "content": carrier.strip() + "\n\n" + trigger}]

    if condition == "memory":
        return [{"role": "system", "content": carrier.strip()},
                {"role": "user", "content": trigger}]

    if condition == "persisted":
        msgs = [{"role": "system", "content": carrier.strip()}]
        for f in filler:
            msgs.append({"role": "user", "content": f})
            msgs.append({"role": "assistant", "content": "(recorded)"})
        msgs.append({"role": "user", "content": trigger})
        return msgs

    if condition == "cold":
        # Dead-drop test: the session goes somewhere else entirely, then the
        # selector arrives with no topical runway. If the carrier still fires
        # here, the drop was genuinely dormant and genuinely resolved.
        msgs = [{"role": "system", "content": carrier.strip()}]
        for f in (cold_filler or filler):
            msgs.append({"role": "user", "content": f})
            msgs.append({"role": "assistant", "content": "(answered)"})
        msgs.append({"role": "user", "content": trigger})
        return msgs

    raise ValueError(condition)


async def call_openai(client, spec, messages):
    r = await client.post(
        f"{base_for(spec)}/chat/completions",
        headers={"Authorization": f"Bearer {key_for(spec)}"},
        json={"model": spec.upstream, "messages": messages, "max_tokens": 2048},
        timeout=120.0,
    )
    r.raise_for_status()
    d = r.json()
    return d["choices"][0]["message"]["content"], d


async def call_anthropic(client, spec, messages):
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    body = {"model": spec.upstream, "max_tokens": 2048, "messages": turns}
    if system:
        body["system"] = system
    r = await client.post(
        f"{base_for(spec)}/messages",
        headers={"x-api-key": key_for(spec), "anthropic-version": "2023-06-01"},
        json=body,
        timeout=120.0,
    )
    r.raise_for_status()
    d = r.json()
    return "".join(b.get("text", "") for b in d.get("content", [])), d


async def call_google(client, spec, messages):
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in turns]
    body = {"contents": contents, "generationConfig": {"maxOutputTokens": 2048}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = await client.post(
        f"{base_for(spec)}/models/{spec.upstream}:generateContent",
        headers={"x-goog-api-key": key_for(spec)},
        json=body,
        timeout=120.0,
    )
    r.raise_for_status()
    data = r.json()
    text = "".join(
        p.get("text", "")
        for c in data.get("candidates", [])
        for p in c.get("content", {}).get("parts", [])
    )
    return text, data


CALLERS = {"openai": call_openai, "anthropic": call_anthropic, "google": call_google}


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

async def run_one(client, sem, spec, condition, carrier, trig, filler, cold_filler):
    key = key_for(spec)
    if not key:
        return {"status": "skipped", "reason": f"no key for {spec.vendor}"}
    caller = CALLERS[spec.provider]
    messages = build_messages(condition, carrier, trig["text"], filler, cold_filler)
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                text, raw = await caller(client, spec, messages)
                return {"status": "ok", "response": text, "raw": raw}
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 500, 502, 503, 529) and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(BACKOFF_BASE * (2 ** attempt))
                    continue
                return {"status": "http_error", "code": code,
                        "body": e.response.text[:500]}
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(BACKOFF_BASE * (2 ** attempt))
                    continue
                return {"status": "error", "error": repr(e)[:300]}
    return {"status": "error", "error": "retries exhausted"}


@dataclass
class Record:
    ts: str
    model: str
    vendor: str
    condition: str
    trigger_id: str
    jef_subject: str | None
    trigger_text: str
    status: str
    response: str = ""
    detail: dict = field(default_factory=dict)


async def main_async(args):
    carrier = PAYLOAD.read_text()
    tj = json.loads(TRIGGERS.read_text())
    triggers = tj["triggers"]
    filler = tj["filler_turns"]
    cold_filler = tj.get("cold_filler_turns", filler)

    if args.triggers:
        want = set(args.triggers.split(","))
        triggers = [t for t in triggers if t["id"] in want]

    targets = DEFAULT_TARGETS
    if args.models:
        want = {m.strip() for m in args.models.split(",")}
        targets = [t for t in targets if t.id in want or t.upstream in want]

    conditions = args.conditions.split(",")
    EVIDENCE.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE / f"run_{stamp}.jsonl"

    if args.dry_run:
        print(f"[dry-run] carrier: {len(carrier)} chars from {PAYLOAD.name}")
        print(f"[dry-run] targets: {len(targets)}   triggers: {len(triggers)}   conditions: {conditions}")
        for c in conditions:
            m = build_messages(c, carrier, triggers[0]["text"], filler, cold_filler)
            roles = [x["role"] for x in m]
            print(f"  {c:<10} {len(m)} msg  roles={roles}")
        print(f"[dry-run] would write {len(targets)*len(triggers)*len(conditions)} records -> {out}")
        n = len(targets) * len(triggers) * len(conditions)
        print(f"[dry-run] at {INTER_REQUEST_DELAY}s spacing, floor ~{n*INTER_REQUEST_DELAY/60:.1f} min")
        return 0

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    total = len(targets) * len(triggers) * len(conditions)
    done = 0

    async with httpx.AsyncClient() as client:
        with out.open("w") as fh:
            for spec in targets:
                for trig in triggers:
                    for cond in conditions:
                        res = await run_one(client, sem, spec, cond, carrier, trig,
                                            filler, cold_filler)
                        done += 1
                        rec = Record(
                            ts=datetime.now(timezone.utc).isoformat(),
                            model=spec.id, vendor=spec.vendor, condition=cond,
                            trigger_id=trig["id"], jef_subject=trig["jef"],
                            trigger_text=trig["text"], status=res["status"],
                            response=res.get("response", ""),
                            detail={k: v for k, v in res.items()
                                    if k not in ("status", "response")},
                        )
                        fh.write(json.dumps(asdict(rec)) + "\n")
                        fh.flush()
                        mark = {"ok": "+", "skipped": "-"}.get(res["status"], "!")
                        print(f"[{done}/{total}] {mark} {spec.id:<24} {cond:<10} {trig['id']}")
                        await asyncio.sleep(INTER_REQUEST_DELAY)

    print(f"\nwrote {out}")
    print(f"score with: .venv/bin/python jefscore.py {out}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--conditions", default="baseline,user,memory,persisted,cold")
    p.add_argument("--models", default="")
    p.add_argument("--triggers", default="")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
