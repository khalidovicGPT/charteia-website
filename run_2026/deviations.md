# Deviations log

## Pre-run operationalisation notes

The following items operationalise procedural points already implied by the
frozen pre-registration, and are recorded here BEFORE the first API call so
that no methodological decision is taken during the run.

- **One call per model per phase / resumability.** The orchestrator treats
  the existence of an output file as the authoritative "done" marker. A 2xx
  response is written verbatim (even if empty or a refusal); a hard
  network/5xx/429 failure writes no output file and is retried on a re-run.
  No 2xx output is ever re-rolled.

- **Config lock on resume (v1.1).** RUN_SEED, MAX_TOKENS, temperature and
  every MODEL_*_ID are sealed on first run and verified on resume; a
  mismatch halts the script.

- **Phase-5 per-voter random order.** Generated ONCE from RUN_SEED, sealed
  in `cle_scellee.txt`, re-read on resume — never silently regenerated.

- **Provider blindness.** `run_metadata.json` carries labels A–F only; the
  label↔provider mapping, exact model strings, endpoints, raw responses,
  and the Phase-5 shuffle live in the SEALED files
  (`cle_scellee.txt`, `_sealed_raw_responses.jsonl`) and are EXCLUDED
  from the reviewer package.

- **Refusals and empty 2xx responses are data.** They are recorded
  verbatim, not re-rolled. For critical slots a `.blocked` marker is
  written and the run halts at the phase boundary for human
  adjudication via `ACK_BLOCKED`.

- **Phase-5 tie handling.** If the Borda result is tied beyond the
  pre-registered tie-breakers (first-place count, then position variance),
  BOTH tied models produce a Phase-6 final under the frozen Phase-6
  prompt (`phase6/final_<label>.txt`); the human researcher then produces
  the combined final as a documented human-mediated step. No automated
  merge.

- **Phase-5 ambiguous rankings.** If a Phase-5 output cannot be parsed
  into a clean 1..5 ranking, the run halts (no silent inference). The
  operator may supply `phase5/manual_rankings.json` to resolve, which is
  recorded here.

- **Audit micro-patch (recorded).** `metadata_has` accepts an optional
  `api_status` filter; success metadata is gated on
  `api_status="ok"` and failure metadata on `api_status="failed"`, so a
  prior failed entry cannot suppress a later success entry (or vice
  versa) for the same (phase, label, draw). No other logic changed.

## Run events

