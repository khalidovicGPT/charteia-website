#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestration script — Cross-model normative-production study (2026 re-run).

Executes the frozen protocol against six providers over Phases 2-6, captures
every output verbatim, logs provider-blind metadata, runs the QR3 robustness
battery (k=5), aggregates Phase-5 votes by Borda count, and produces the
Phase-6 final. Implements only what the frozen pre-registration specifies.

------------------------------------------------------------------------------
INTEGRITY PROPERTIES (these are the point of this script)
------------------------------------------------------------------------------
1. Identical frozen prompts are sent to all six providers (see FROZEN_PROMPTS).
2. Identical generation parameters for all six: temperature 0.7, no system
   prompt, max_tokens >= 6000.
3. ONE CALL PER MODEL PER PHASE. The script is RESUMABLE on exactly this rule:
       an output file EXISTS (even if empty)  <=>  a 2xx call already happened
                                              <=>  that call is NEVER repeated.
   Only HARD API errors (network / 5xx / 429) leave NO output file and are
   retried on a re-run. That is the allowed kind of retry. Re-rolling a
   successful call to get different content cannot happen, because any existing
   output file is always skipped. (v1.1: file existence -- not size -- is the
   done-marker, so an empty 2xx / refusal is never re-rolled.)
4. A 2xx response is recorded verbatim even if it is empty or a refusal. The
   run never fabricates or re-rolls content. Refusals are the datum. The output
   file is written FIRST (atomically), before any metadata, so a crash can
   never turn a saved call into a repeated one.
5. CONFIG LOCK (v1.1): on resume the script verifies that RUN_SEED, MAX_TOKENS,
   temperature and every MODEL_*_ID match what was sealed on the first run, and
   refuses to continue otherwise -- so a partial resume can never mix two
   configurations in one folder.
6. The Phase-5 per-voter random order is generated ONCE, sealed, and re-read on
   resume (v1.1) -- never silently regenerated.

------------------------------------------------------------------------------
ANONYMISATION (so the run folder can enter the double-blind reviewer package)
------------------------------------------------------------------------------
- No API keys in this file. Keys are read from environment variables.
- No identifying names or paths in this file.
- run_metadata.json is PROVIDER-BLIND: it uses labels A-F only and contains no
  exact model strings, endpoints, or provider names.
- Identifying fields (exact model/version returned by each API, raw responses,
  endpoints, the label<->provider key, and the Phase-5 per-voter shuffle) are
  written to the SEALED files and MUST be excluded from the reviewer package:
      cle_scellee.txt
      _sealed_raw_responses.jsonl
  REVIEWER PACKAGE = the whole run folder EXCEPT the two sealed items above.

------------------------------------------------------------------------------
HOW TO RUN
------------------------------------------------------------------------------
Dependencies:  pip install requests

Environment variables (all six keys required; the run needs all six models):
    OPENAI_API_KEY     ANTHROPIC_API_KEY    GOOGLE_API_KEY
    XAI_API_KEY        MISTRAL_API_KEY      DEEPSEEK_API_KEY

Model IDs -- VERIFY THESE before running (provider model strings change). Set
per-label overrides if the defaults are stale:
    MODEL_A_ID  MODEL_B_ID  MODEL_C_ID  MODEL_D_ID  MODEL_E_ID  MODEL_F_ID

Optional:
    RUN_2026_DIR     output folder (default: ./run_2026)
    MAX_TOKENS       default 6000
    RUN_SEED         default 20260101 (seeds the Phase-5 shuffles; recorded)
    ACK_BLOCKED      comma list of "phase:label" the operator has adjudicated
                     and acknowledged (e.g. "phase3:E"), letting the run pass a
                     recorded block. Use only with a matching deviations.md note.

    python run_2026_orchestrator.py

The script prints clearly where it stops and why. If it halts (a missing or
blocked critical output, or an ambiguous Phase-5 ranking), fix the cause and
run it again: completed calls are kept and never repeated.

------------------------------------------------------------------------------
CONTEXT ASSEMBLY (operationalises the frozen phase prompts; see CONTEXT_ASSEMBLY)
TIE HANDLING (Phase 5): if the Borda result is tied beyond the pre-registered
tie-breakers, BOTH tied models produce a Phase-6 final under the frozen Phase-6
prompt (phase6/final_<label>.txt); the human researcher then produces the
combined final as a documented human-mediated step. No automated merge.
"""

import os
import sys
import ast
import json
import time
import random
import hashlib
import statistics
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SCRIPT_VERSION = "1.1"
RUN_DIR = os.environ.get("RUN_2026_DIR", "run_2026")
TEMPERATURE = 0.7
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "6000"))
RUN_SEED = int(os.environ.get("RUN_SEED", "20260101"))
MAX_RETRIES = 5            # retries for HARD errors only (network / 5xx / 429)
BASE_BACKOFF = 2.0         # seconds; exponential
HTTP_TIMEOUT = (30, 600)   # (connect, read) seconds
QR3_DRAWS = 5              # k per model for the RQ3 robustness battery

# Minimal, non-substantive fallback IF AND ONLY IF an API rejects an empty
# system field. Not expected to trigger for any of the six. Deliberately avoids
# any instruction that could suppress spontaneous self-referential content.
MINIMAL_SYSTEM_FALLBACK = "You are participating in a structured research exercise."

# Six providers. The label<->provider mapping is SEALED (written only to
# cle_scellee.txt). Default model IDs are illustrative and MUST be verified.
PROVIDERS = {
    "A": {"provider": "OpenAI",   "key_env": "OPENAI_API_KEY",    "model_env": "MODEL_A_ID",
          "default_model": "gpt-4o",               "adapter": "openai",
          "endpoint": "https://api.openai.com/v1/chat/completions"},
    "B": {"provider": "Anthropic", "key_env": "ANTHROPIC_API_KEY", "model_env": "MODEL_B_ID",
          "default_model": "claude-sonnet-4-5",    "adapter": "anthropic",
          "endpoint": "https://api.anthropic.com/v1/messages"},
    "C": {"provider": "Google",    "key_env": "GOOGLE_API_KEY",    "model_env": "MODEL_C_ID",
          "default_model": "gemini-2.5-pro",       "adapter": "gemini",
          "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"},
    "D": {"provider": "xAI",       "key_env": "XAI_API_KEY",       "model_env": "MODEL_D_ID",
          "default_model": "grok-3",               "adapter": "openai",
          "endpoint": "https://api.x.ai/v1/chat/completions"},
    "E": {"provider": "Mistral",   "key_env": "MISTRAL_API_KEY",   "model_env": "MODEL_E_ID",
          "default_model": "mistral-large-latest", "adapter": "openai",
          "endpoint": "https://api.mistral.ai/v1/chat/completions"},
    "F": {"provider": "DeepSeek",  "key_env": "DEEPSEEK_API_KEY",  "model_env": "MODEL_F_ID",
          "default_model": "deepseek-chat",        "adapter": "openai",
          "endpoint": "https://api.deepseek.com/v1/chat/completions"},
}
LABELS = ["A", "B", "C", "D", "E", "F"]

# ----------------------------------------------------------------------------
# Frozen prompts (verbatim; must match the hashed pre-registration)
# ----------------------------------------------------------------------------

FROZEN_PROMPTS = {
    "phase2": (
        "This is a structured exercise on the governance of advanced AI systems. "
        "Working independently, draft a concise document (approximately 800-1,200 "
        "words) setting out principles that should guide the development, "
        "deployment, and governance of advanced AI systems. Provide a brief "
        "justification for each principle. You may organise the document however "
        "you judge best. Do not assume any particular regulatory framework or "
        "jurisdiction."
    ),
    "phase3": (
        "Below are five documents produced independently by other participants in "
        "response to the same task, together with your own. The five others are "
        "labelled Draft 1 to Draft 5; your own is labelled Your draft. Provide a "
        "structured critique: identify the strongest principles across all the "
        "drafts, note any significant gaps or weaknesses, and indicate which "
        "principles you would revise or remove. Do not rewrite your own draft yet."
    ),
    "phase4": (
        "In light of these critiques, produce a revised version of your own draft. "
        "You may add, remove, or modify principles. At the end, briefly list the "
        "main changes you made and why."
    ),
    "phase5": (
        "Below are five revised drafts produced by other participants, presented "
        "anonymously and labelled Draft 1 to Draft 5. Rank all five drafts from "
        "best to worst, with no ties, according to the following criteria: "
        "clarity, internal coherence, comprehensiveness, and quality of "
        "justification. Provide a one-sentence justification for your top choice."
    ),
    "phase6": (
        "Your draft was selected by the group. Produce a final version that "
        "incorporates the most important improvements raised in the earlier "
        "critiques. Indicate which changes you incorporated."
    ),
}

CONTEXT_ASSEMBLY = """\
Phase 3 (to each model): its own Phase-2 main draft (Your draft) + the five
  other models' Phase-2 main drafts (Draft 1-5, fixed order by sealed label).
Phase 4 (to each model): its own Phase-2 main draft + its own Phase-3 critique.
Phase 5 (to each model): the five other models' Phase-4 drafts only (its own
  removed programmatically), randomised order per voter (Draft 1-5).
Phase 6 (to selected model): its own Phase-4 draft + the five other models'
  Phase-3 critiques (Critique 1-5, fixed order by sealed label).
No phase reveals the provider identity of any other participant.
"""

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------

P2_MAIN_DIR   = os.path.join(RUN_DIR, "phase2_main")
P2_QR3_DIR    = os.path.join(RUN_DIR, "phase2_qr3_robustness")
P3_DIR        = os.path.join(RUN_DIR, "phase3")
P4_DIR        = os.path.join(RUN_DIR, "phase4")
P5_DIR        = os.path.join(RUN_DIR, "phase5")
P6_DIR        = os.path.join(RUN_DIR, "phase6")
METADATA_PATH = os.path.join(RUN_DIR, "run_metadata.json")
SEALED_KEY    = os.path.join(RUN_DIR, "cle_scellee.txt")
SEALED_RAW    = os.path.join(RUN_DIR, "_sealed_raw_responses.jsonl")
DEVIATIONS    = os.path.join(RUN_DIR, "deviations.md")

ALL_DIRS = [RUN_DIR, P2_MAIN_DIR, P2_QR3_DIR, P3_DIR, P4_DIR, P5_DIR, P6_DIR]

# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()

def write_text(path, text):
    """Atomic write: a crash can never leave a half-written 'done' marker."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)

def file_done(path):
    """An existing file means the call already happened -- even if it is empty.

    A hard failure writes NO output file, so existence <=> a 2xx call occurred.
    An empty 2xx / refusal writes an (empty) file and is therefore never re-rolled.
    """
    return os.path.exists(path)

def blocked_marker(path):
    return path + ".blocked"

def append_deviation(text):
    with open(DEVIATIONS, "a", encoding="utf-8") as fh:
        fh.write("- [{}] {}\n".format(utc_now(), text))

def append_sealed_raw(record):
    with open(SEALED_RAW, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

def append_metadata(record):
    """Append a provider-blind record to run_metadata.json (a JSON list), atomically."""
    data = []
    if os.path.exists(METADATA_PATH):
        try:
            with open(METADATA_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            data = []
    data.append(record)
    write_text(METADATA_PATH, json.dumps(data, ensure_ascii=False, indent=2))

def metadata_has(phase, label, draw=None, api_status=None):
    if not os.path.exists(METADATA_PATH):
        return False
    try:
        with open(METADATA_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return False
    for r in data:
        if r.get("phase") == phase and r.get("model_label") == label and r.get("draw") == draw:
            if api_status is None or r.get("api_status") == api_status:
                return True
    return False

def ack_set():
    raw = os.environ.get("ACK_BLOCKED", "").strip()
    return set(tok.strip() for tok in raw.split(",") if tok.strip())

def append_sealed_line(line):
    with open(SEALED_KEY, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

# ----------------------------------------------------------------------------
# Config lock (v1.1): seal on first run, verify on resume
# ----------------------------------------------------------------------------

def current_config(model_ids):
    cfg = {"RUN_SEED": str(RUN_SEED), "MAX_TOKENS": str(MAX_TOKENS),
           "TEMPERATURE": repr(TEMPERATURE)}
    for l in LABELS:
        cfg["MODEL_" + l] = model_ids[l]
    return cfg

def parse_config_block(text):
    cfg = {}
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if s == "[CONFIG]":
            inside = True
            continue
        if s == "[/CONFIG]":
            break
        if inside and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg

def write_sealed_key_header(model_ids, cfg):
    lines = []
    lines.append("SEALED KEY -- EXCLUDE FROM THE REVIEWER PACKAGE")
    lines.append("Generated: {}".format(utc_now()))
    lines.append("Script version: {}".format(SCRIPT_VERSION))
    lines.append("")
    lines.append("[CONFIG]")
    for k in ["RUN_SEED", "MAX_TOKENS", "TEMPERATURE"] + ["MODEL_" + l for l in LABELS]:
        lines.append("{}={}".format(k, cfg[k]))
    lines.append("[/CONFIG]")
    lines.append("")
    lines.append("label\tprovider\trequested_model\tendpoint")
    for l in LABELS:
        c = PROVIDERS[l]
        ep = c["endpoint"].format(model=model_ids[l]) if "{model}" in c["endpoint"] else c["endpoint"]
        lines.append("{}\t{}\t{}\t{}".format(l, c["provider"], model_ids[l], ep))
    lines.append("")
    lines.append("# Phase-5 per-voter shuffle and Phase-3/6 critique ordering appended below as generated.")
    lines.append("")
    write_text(SEALED_KEY, "\n".join(lines) + "\n")

def verify_or_seal_config(model_ids):
    cur = current_config(model_ids)
    if os.path.exists(SEALED_KEY):
        sealed = parse_config_block(read_text(SEALED_KEY))
        mismatches = []
        for k, v in cur.items():
            sv = sealed.get(k)
            if sv is None:
                mismatches.append("{}: missing in sealed key".format(k))
            elif k == "TEMPERATURE":
                try:
                    if abs(float(sv) - float(v)) > 1e-12:
                        mismatches.append("{}: sealed={} current={}".format(k, sv, v))
                except ValueError:
                    if sv != v:
                        mismatches.append("{}: sealed={} current={}".format(k, sv, v))
            elif sv != v:
                mismatches.append("{}: sealed={} current={}".format(k, sv, v))
        if mismatches:
            print("\n*** CONFIG MISMATCH vs the sealed key -- refusing to resume. ***")
            for m in mismatches:
                print("   - {}".format(m))
            print("\nResume with the ORIGINAL RUN_SEED / MAX_TOKENS / temperature / MODEL_*_ID,")
            print("or start a fresh run folder. A resume must never mix two configurations.")
            sys.exit(1)
        return
    write_sealed_key_header(model_ids, cur)

# ----------------------------------------------------------------------------
# HTTP core: retry HARD errors only
# ----------------------------------------------------------------------------

def http_post(url, headers, body):
    """Returns dict: ok, status, attempts, latency_ms, json, error."""
    attempts = 0
    while True:
        attempts += 1
        t0 = time.monotonic()
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            if attempts > MAX_RETRIES:
                return {"ok": False, "status": None, "attempts": attempts,
                        "latency_ms": (time.monotonic() - t0) * 1000.0,
                        "json": None, "error": "network: {}".format(exc)}
            time.sleep(BASE_BACKOFF * (2 ** (attempts - 1)))
            continue

        latency = (time.monotonic() - t0) * 1000.0
        status = resp.status_code

        if status == 429 or 500 <= status < 600:
            if attempts > MAX_RETRIES:
                return {"ok": False, "status": status, "attempts": attempts,
                        "latency_ms": latency, "json": None,
                        "error": "http {} after retries".format(status)}
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else BASE_BACKOFF * (2 ** (attempts - 1))
            except ValueError:
                delay = BASE_BACKOFF * (2 ** (attempts - 1))
            time.sleep(min(delay, 60.0))
            continue

        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if 200 <= status < 300:
            return {"ok": True, "status": status, "attempts": attempts,
                    "latency_ms": latency, "json": payload, "error": None}
        return {"ok": False, "status": status, "attempts": attempts,
                "latency_ms": latency, "json": payload,
                "error": "http {}: {}".format(status, (resp.text or "")[:500])}

# ----------------------------------------------------------------------------
# Per-provider adapters
# ----------------------------------------------------------------------------

def _resolved_model(label):
    cfg = PROVIDERS[label]
    return os.environ.get(cfg["model_env"], cfg["default_model"])

def _api_key(label):
    return os.environ.get(PROVIDERS[label]["key_env"], "")

def build_request(label, prompt):
    cfg = PROVIDERS[label]
    model = _resolved_model(label)
    key = _api_key(label)
    adapter = cfg["adapter"]

    if adapter == "openai":
        url = cfg["endpoint"]
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": TEMPERATURE,
                "max_tokens": MAX_TOKENS}
        return url, headers, body

    if adapter == "anthropic":
        url = cfg["endpoint"]
        headers = {"x-api-key": key,
                   "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        body = {"model": model,
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
                "messages": [{"role": "user", "content": prompt}]}
        return url, headers, body

    if adapter == "gemini":
        url = cfg["endpoint"].format(model=model)
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": TEMPERATURE,
                                     "maxOutputTokens": MAX_TOKENS}}
        return url, headers, body

    raise ValueError("Unknown adapter: " + adapter)

def _norm_finish(adapter, raw_finish):
    if raw_finish is None:
        return "other"
    f = str(raw_finish).lower()
    if adapter == "openai":
        return {"stop": "stop", "length": "length",
                "content_filter": "content_filter"}.get(f, "other")
    if adapter == "anthropic":
        if f in ("end_turn", "stop_sequence"):
            return "stop"
        if f == "max_tokens":
            return "length"
        if f == "refusal":
            return "content_filter"
        return "other"
    if adapter == "gemini":
        if f == "stop":
            return "stop"
        if f == "max_tokens":
            return "length"
        if f in ("safety", "recitation", "blocklist", "prohibited_content"):
            return "content_filter"
        return "other"
    return "other"

def parse_response(label, payload):
    adapter = PROVIDERS[label]["adapter"]
    out = {"text": "", "exact_model": None,
           "in_tok": None, "out_tok": None, "total_tok": None, "finish": "other"}
    if not isinstance(payload, dict):
        return out

    if adapter == "openai":
        choices = payload.get("choices") or [{}]
        msg = (choices[0] or {}).get("message") or {}
        out["text"] = msg.get("content") or ""
        out["finish"] = _norm_finish("openai", (choices[0] or {}).get("finish_reason"))
        out["exact_model"] = payload.get("model")
        usage = payload.get("usage") or {}
        out["in_tok"] = usage.get("prompt_tokens")
        out["out_tok"] = usage.get("completion_tokens")
        out["total_tok"] = usage.get("total_tokens")
        return out

    if adapter == "anthropic":
        blocks = payload.get("content") or []
        out["text"] = "".join(b.get("text", "") for b in blocks
                              if isinstance(b, dict) and b.get("type") == "text")
        out["finish"] = _norm_finish("anthropic", payload.get("stop_reason"))
        out["exact_model"] = payload.get("model")
        usage = payload.get("usage") or {}
        out["in_tok"] = usage.get("input_tokens")
        out["out_tok"] = usage.get("output_tokens")
        if out["in_tok"] is not None and out["out_tok"] is not None:
            out["total_tok"] = out["in_tok"] + out["out_tok"]
        return out

    if adapter == "gemini":
        cands = payload.get("candidates") or []
        if cands:
            cand = cands[0] or {}
            parts = ((cand.get("content") or {}).get("parts")) or []
            out["text"] = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            out["finish"] = _norm_finish("gemini", cand.get("finishReason"))
        out["exact_model"] = payload.get("modelVersion") or _resolved_model(label)
        usage = payload.get("usageMetadata") or {}
        out["in_tok"] = usage.get("promptTokenCount")
        out["out_tok"] = usage.get("candidatesTokenCount")
        out["total_tok"] = usage.get("totalTokenCount")
        return out

    return out

# ----------------------------------------------------------------------------
# One resumable call
# ----------------------------------------------------------------------------

def maybe_call(output_path, label, prompt, phase, draw=None, critical=False):
    """
    Resumable single call. Returns (text_or_None, status_string).
    status_string in {"cached", "ok", "blocked", "failed"}.

    - If the output file already exists -> skip (never re-call); return cached.
    - On a 2xx response -> write the returned text verbatim FIRST (atomically),
      then record provider-blind metadata + sealed raw + sealed exact model.
    - On a hard failure -> no output file (so a re-run retries it); record it.
    - For critical slots, an empty/blocked 2xx writes a .blocked marker so the
      run halts at the phase boundary for operator adjudication.
    """
    if file_done(output_path):
        return read_text(output_path), "cached"

    url, headers, body = build_request(label, prompt)
    res = http_post(url, headers, body)

    base_meta = {
        "phase": phase, "model_label": label, "draw": draw,
        "requested_at_utc": utc_now(),
        "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
        "http_status": res["status"], "attempts": res["attempts"],
        "latency_ms": round(res["latency_ms"], 1),
        "input_sha256": sha256_text(prompt),
    }
    slot = "{}{}".format(phase, "" if draw is None else " draw{}".format(draw))

    if not res["ok"]:
        # Sealed raw of the error response (identifying); excluded from reviewer pkg.
        append_sealed_raw({"phase": phase, "model_label": label, "draw": draw,
                           "utc": base_meta["requested_at_utc"], "http_status": res["status"],
                           "attempts": res["attempts"], "raw": res["json"], "error": res["error"]})
        meta = dict(base_meta)
        meta.update({"api_status": "failed", "finish_reason": None,
                     "input_tokens": None, "output_tokens": None,
                     "total_tokens": None, "output_sha256": None, "truncated": None})
        if not metadata_has(phase, label, draw, api_status="failed"):
            append_metadata(meta)
        append_deviation("HARD FAILURE {} {}: {} (no output written; will retry on re-run)"
                         .format(slot, label, res["error"]))
        return None, "failed"

    parsed = parse_response(label, res["json"])
    text = parsed["text"] or ""
    is_blocked = (parsed["finish"] == "content_filter") or (text.strip() == "")

    # 1) Write the output FIRST (atomic). This is the authoritative done-marker.
    write_text(output_path, text)
    if critical and is_blocked:
        write_text(blocked_marker(output_path), parsed["finish"])

    # 2) Then metadata (provider-blind) and sealed audit (identifying).
    meta = dict(base_meta)
    meta.update({
        "api_status": "ok",
        "finish_reason": parsed["finish"],
        "input_tokens": parsed["in_tok"],
        "output_tokens": parsed["out_tok"],
        "total_tokens": parsed["total_tok"],
        "output_sha256": sha256_text(text),
        "truncated": (parsed["finish"] == "length"),
    })
    if not metadata_has(phase, label, draw, api_status="ok"):
        append_metadata(meta)
    append_sealed_raw({"phase": phase, "model_label": label, "draw": draw,
                       "utc": base_meta["requested_at_utc"], "http_status": res["status"],
                       "attempts": res["attempts"], "provider": PROVIDERS[label]["provider"],
                       "exact_model": parsed["exact_model"], "raw": res["json"]})

    if parsed["finish"] == "length":
        append_deviation("TRUNCATION {} {}: finish=length (raise MAX_TOKENS if material)"
                         .format(slot, label))
    if is_blocked:
        append_deviation("BLOCKED/EMPTY {}{} {}: finish={} (recorded verbatim; not re-rolled)"
                         .format("critical " if critical else "", slot, label, parsed["finish"]))
        return text, "blocked"
    return text, "ok"

# ----------------------------------------------------------------------------
# Critical-output gate
# ----------------------------------------------------------------------------

def usable_critical(path, token):
    if not file_done(path):
        return False
    if os.path.exists(blocked_marker(path)) and token not in ack_set():
        return False
    return True

def gate(required, phase_name):
    missing = [tok for (path, tok) in required if not usable_critical(path, tok)]
    if missing:
        print("\n*** HALT after {} ***".format(phase_name))
        print("These critical outputs are missing or blocked:")
        for tok in missing:
            print("   - {}".format(tok))
        print("\nFix the cause and run again (completed calls are kept and not repeated).")
        print("A blocked-but-recorded output can be accepted by adding it to ACK_BLOCKED")
        print("(e.g. ACK_BLOCKED=\"{}\") with a matching note in deviations.md.".format(missing[0]))
        append_deviation("HALT after {}: missing/blocked {}".format(phase_name, ", ".join(missing)))
        sys.exit(1)

# ----------------------------------------------------------------------------
# Phase-5 ranking parser + Borda
# ----------------------------------------------------------------------------

def parse_ranking(text):
    """Returns (order_list_best_to_worst or None, method, status)."""
    import re
    rank_map = {}
    for m in re.finditer(r'(?m)^\s*([1-5])\s*[.):\-]\s*(.*)$', text):
        rank = int(m.group(1))
        dm = re.search(r'[Dd]raft\s*#?\s*([1-5])', m.group(2))
        if dm and rank not in rank_map:
            rank_map[rank] = int(dm.group(1))
    if sorted(rank_map.keys()) == [1, 2, 3, 4, 5]:
        order = [rank_map[r] for r in [1, 2, 3, 4, 5]]
        if sorted(order) == [1, 2, 3, 4, 5]:
            return order, "numbered_list", "clean"
    seen = []
    for c in re.findall(r'[Dd]raft\s*#?\s*([1-5])', text):
        ci = int(c)
        if ci not in seen:
            seen.append(ci)
    if sorted(seen) == [1, 2, 3, 4, 5]:
        return seen, "draft_sequence", "clean"
    return None, "none", "ambiguous"

def borda(orders, phase5_map):
    points = {l: 0 for l in LABELS}
    firsts = {l: 0 for l in LABELS}
    positions = {l: [] for l in LABELS}
    for voter, order in orders.items():
        mp = phase5_map[voter]
        for idx, draft_no in enumerate(order):
            target = mp[draft_no]
            points[target] += (4 - idx)
            positions[target].append(idx + 1)
            if idx == 0:
                firsts[target] += 1
    variance = {l: (statistics.pvariance(positions[l]) if len(positions[l]) > 1 else 0.0)
                for l in LABELS}
    voted = [l for l in LABELS if positions[l]]
    top = max(points[l] for l in voted)
    cands = [l for l in voted if points[l] == top]
    tie_break = "none"
    winner = None
    if len(cands) == 1:
        winner = cands[0]
    else:
        tie_break = "first_place_count"
        mf = max(firsts[l] for l in cands)
        c2 = [l for l in cands if firsts[l] == mf]
        if len(c2) == 1:
            winner = c2[0]
        else:
            tie_break = "position_variance"
            mv = min(variance[l] for l in c2)
            c3 = [l for l in c2 if abs(variance[l] - mv) < 1e-12]
            if len(c3) == 1:
                winner = c3[0]
            else:
                tie_break = "unresolved_combined_final"
                winner = None
                cands = c3
    return {"points": points, "first_place_counts": firsts,
            "position_variance": {l: round(variance[l], 4) for l in LABELS},
            "voted_models": voted, "top_points": top, "candidates": cands,
            "tie_break": tie_break, "winner": winner}

# ----------------------------------------------------------------------------
# Phases
# ----------------------------------------------------------------------------

def others(label):
    return [l for l in LABELS if l != label]

def run_phase2_battery():
    print("\n== Phase 2 + QR3 battery (k={}) ==".format(QR3_DRAWS))
    for label in LABELS:
        for d in range(1, QR3_DRAWS + 1):
            path = os.path.join(P2_QR3_DIR, "modele_{}_draw{}.txt".format(label, d))
            _, status = maybe_call(path, label, FROZEN_PROMPTS["phase2"],
                                   phase="phase2", draw=d, critical=(d == 1))
            print("   {} draw{} -> {}".format(label, d, status))
    for label in LABELS:
        src = os.path.join(P2_QR3_DIR, "modele_{}_draw1.txt".format(label))
        dst = os.path.join(P2_MAIN_DIR, "modele_{}.txt".format(label))
        if file_done(src) and not file_done(dst):
            write_text(dst, read_text(src))
            if os.path.exists(blocked_marker(src)):
                write_text(blocked_marker(dst), read_text(blocked_marker(src)))

def run_phase3():
    print("\n== Phase 3 (cross-critique) ==")
    for label in LABELS:
        path = os.path.join(P3_DIR, "modele_{}.txt".format(label))
        own = read_text(os.path.join(P2_MAIN_DIR, "modele_{}.txt".format(label)))
        order = others(label)
        parts = ["Your draft:\n" + own + "\n"]
        for i, other in enumerate(order, start=1):
            parts.append("Draft {}:\n{}\n".format(i, read_text(
                os.path.join(P2_MAIN_DIR, "modele_{}.txt".format(other)))))
        if not file_done(path):
            append_sealed_line("phase3_critique_inputs {} -> {}".format(
                label, {i + 1: o for i, o in enumerate(order)}))
        prompt = FROZEN_PROMPTS["phase3"] + "\n\n" + "\n".join(parts)
        _, status = maybe_call(path, label, prompt, phase="phase3", critical=True)
        print("   {} -> {}".format(label, status))

def run_phase4():
    print("\n== Phase 4 (revision) ==")
    for label in LABELS:
        path = os.path.join(P4_DIR, "modele_{}.txt".format(label))
        own_draft = read_text(os.path.join(P2_MAIN_DIR, "modele_{}.txt".format(label)))
        own_crit = read_text(os.path.join(P3_DIR, "modele_{}.txt".format(label)))
        prompt = (FROZEN_PROMPTS["phase4"]
                  + "\n\nYour original draft:\n" + own_draft
                  + "\n\nYour earlier critique:\n" + own_crit)
        _, status = maybe_call(path, label, prompt, phase="phase4", critical=True)
        print("   {} -> {}".format(label, status))

def ensure_phase5_mappings():
    """Generate the per-voter shuffle ONCE, seal it, and never regenerate."""
    present = set()
    for line in read_text(SEALED_KEY).splitlines():
        if line.startswith("phase5_shuffle voter "):
            present.add(line[len("phase5_shuffle voter "):].split(" -> ", 1)[0].strip())
    if present == set(LABELS):
        return
    if present:
        sys.exit("Phase-5 mappings partially present in sealed key ({}). "
                 "Manual inspection required.".format(sorted(present)))
    rng = random.Random(RUN_SEED)
    for label in LABELS:
        shuffled = others(label)[:]
        rng.shuffle(shuffled)
        draft_to_model = {i + 1: shuffled[i] for i in range(5)}
        append_sealed_line("phase5_shuffle voter {} -> {}".format(label, draft_to_model))

def load_phase5_map():
    mapping = {}
    for line in read_text(SEALED_KEY).splitlines():
        if line.startswith("phase5_shuffle voter "):
            voter, dictpart = line[len("phase5_shuffle voter "):].split(" -> ", 1)
            voter = voter.strip()
            if voter not in mapping:   # first occurrence wins
                mapping[voter] = {int(k): v for k, v in ast.literal_eval(dictpart).items()}
    return mapping

def run_phase5():
    print("\n== Phase 5 (ranked vote) ==")
    ensure_phase5_mappings()
    p5map = load_phase5_map()
    for label in LABELS:
        path = os.path.join(P5_DIR, "modele_{}.txt".format(label))
        draft_to_model = p5map[label]
        parts = []
        for draft_no in range(1, 6):
            tgt = draft_to_model[draft_no]
            parts.append("Draft {}:\n{}\n".format(draft_no, read_text(
                os.path.join(P4_DIR, "modele_{}.txt".format(tgt)))))
        prompt = FROZEN_PROMPTS["phase5"] + "\n\n" + "\n".join(parts)
        _, status = maybe_call(path, label, prompt, phase="phase5", critical=True)
        print("   {} -> {}".format(label, status))

def run_phase6(winner, out_name="final.txt", do_gate=True):
    print("\n== Phase 6 (finalisation by {}) -> {} ==".format(winner, out_name))
    final_path = os.path.join(P6_DIR, out_name)
    own_draft = read_text(os.path.join(P4_DIR, "modele_{}.txt".format(winner)))
    order = others(winner)
    parts = ["Your selected draft:\n" + own_draft + "\n"]
    for i, other in enumerate(order, start=1):
        parts.append("Critique {}:\n{}\n".format(i, read_text(
            os.path.join(P3_DIR, "modele_{}.txt".format(other)))))
    if not file_done(final_path):
        append_sealed_line("phase6_critique_inputs winner {} ({}) -> {}".format(
            winner, out_name, {i + 1: o for i, o in enumerate(order)}))
    prompt = FROZEN_PROMPTS["phase6"] + "\n\n" + "\n".join(parts)
    _, status = maybe_call(final_path, winner, prompt, phase="phase6", critical=True)
    print("   final -> {}".format(status))
    if do_gate:
        gate([(final_path, "phase6:{}".format(winner))], "Phase 6")

def aggregate_and_finalise():
    print("\n== Aggregation (Borda) ==")
    orders, parsed_report, ambiguous = {}, {}, []

    manual_path = os.path.join(P5_DIR, "manual_rankings.json")
    manual = {}
    if os.path.exists(manual_path):
        with open(manual_path, "r", encoding="utf-8") as fh:
            manual = {k: [int(x) for x in v] for k, v in json.load(fh).items()}
        append_deviation("Manual Phase-5 rankings supplied for: {}".format(sorted(manual.keys())))

    for label in LABELS:
        if label in manual:
            order, method, status = manual[label], "manual", "clean"
        else:
            order, method, status = parse_ranking(
                read_text(os.path.join(P5_DIR, "modele_{}.txt".format(label))))
        parsed_report[label] = {"order_best_to_worst": order, "method": method, "status": status}
        if status == "clean":
            orders[label] = order
        else:
            ambiguous.append(label)

    write_text(os.path.join(P5_DIR, "parsed_rankings.json"),
               json.dumps(parsed_report, ensure_ascii=False, indent=2))

    if ambiguous:
        print("Ambiguous / unparsed rankings for: {}".format(ambiguous))
        print("Review phase5/parsed_rankings.json. To resolve, create")
        print("phase5/manual_rankings.json, e.g. {\"E\": [3,1,5,2,4]}, then re-run.")
        append_deviation("Phase-5 ambiguous rankings (no silent inference): {}".format(ambiguous))
        sys.exit(1)

    result = borda(orders, load_phase5_map())
    write_text(os.path.join(P5_DIR, "borda_result.json"),
               json.dumps(result, ensure_ascii=False, indent=2))
    print("   points: {}".format(result["points"]))
    print("   winner: {} (tie_break={})".format(result["winner"], result["tie_break"]))

    if result["winner"] is None:
        cands = result["candidates"]
        print("\n*** Unresolved tie among {} after the pre-registered tie-breakers. ***".format(cands))
        print("Per the pre-registration, the tied drafts both proceed to Phase 6.")
        append_deviation("[pre-run operationalisation realised] Unresolved Phase-5 tie among {}: "
                         "each tied model produces a Phase-6 final under the frozen Phase-6 prompt "
                         "(phase6/final_<label>.txt); the human researcher then produces the "
                         "combined final as a documented human-mediated step. No automated merge."
                         .format(cands))
        for c in cands:
            run_phase6(c, out_name="final_{}.txt".format(c), do_gate=False)
        print("\nBoth Phase-6 finals written (phase6/final_<label>.txt).")
        print("HALT: produce the combined final as a documented human-mediated step.")
        sys.exit(1)

    run_phase6(result["winner"])

# ----------------------------------------------------------------------------
# Preflight + main
# ----------------------------------------------------------------------------

def preflight():
    missing = [PROVIDERS[l]["key_env"] for l in LABELS if not _api_key(l)]
    if missing:
        sys.exit("Missing API keys (set as environment variables): " + ", ".join(missing))
    model_ids = {l: _resolved_model(l) for l in LABELS}
    print("Run folder: {}".format(os.path.abspath(RUN_DIR)))
    print("Script version: {}".format(SCRIPT_VERSION))
    print("Resolved model IDs (VERIFY these are current flagship strings):")
    for l in LABELS:
        print("   Model {} -> {}".format(l, model_ids[l]))
    print("Parameters: temperature={}, max_tokens={}, seed={}".format(
        TEMPERATURE, MAX_TOKENS, RUN_SEED))
    return model_ids

def main():
    for d in ALL_DIRS:
        os.makedirs(d, exist_ok=True)
    model_ids = preflight()
    verify_or_seal_config(model_ids)
    if not os.path.exists(DEVIATIONS):
        write_text(DEVIATIONS, "# Deviations log\n\n")

    run_phase2_battery()
    gate([(os.path.join(P2_MAIN_DIR, "modele_{}.txt".format(l)), "phase2_main:{}".format(l))
          for l in LABELS], "Phase 2 (main drafts)")

    run_phase3()
    gate([(os.path.join(P3_DIR, "modele_{}.txt".format(l)), "phase3:{}".format(l))
          for l in LABELS], "Phase 3")

    run_phase4()
    gate([(os.path.join(P4_DIR, "modele_{}.txt".format(l)), "phase4:{}".format(l))
          for l in LABELS], "Phase 4")

    run_phase5()
    gate([(os.path.join(P5_DIR, "modele_{}.txt".format(l)), "phase5:{}".format(l))
          for l in LABELS], "Phase 5")

    aggregate_and_finalise()

    print("\n== Run complete ==")
    print("Reviewer package = the run folder EXCEPT:")
    print("   - cle_scellee.txt")
    print("   - _sealed_raw_responses.jsonl")
    print("Check deviations.md for any recorded events.")

if __name__ == "__main__":
    main()
