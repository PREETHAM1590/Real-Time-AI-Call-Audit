# Kaggle GPU benchmark harness (pre-provisioning, not production infrastructure)

Status: written, **not yet executed**. No benchmark numbers exist for this repository until
this harness is run and its output is transcribed into
[`docs/operations-and-evaluation.md`](../../docs/operations-and-evaluation.md).

## What this is

A one-off, disposable benchmark that answers "roughly how fast and how accurate-shaped are
faster-whisper and the Qwen3-8B audit candidate on a free GPU", before dedicated hardware is
available. It exists to produce the *Local model contract* evidence row in the operations
guide's release-validation table: segment finalisation, timing, JSON-schema validity, GPU
memory, and latency — measured once, on synthetic-only fixtures, so a real provisioning
decision has a number behind it instead of a guess.

## What this is **not**

- **Not a deployment.** Kaggle notebook sessions are ephemeral (session and weekly GPU-quota
  limits) and are not "infrastructure controlled by this project" per `AGENTS.md`. Nothing
  here is wired into the running application, and no call audio or real transcript is ever
  sent to it. The application's own adapters (`app/transcription.py`, `app/audit.py`) stay
  bound to loopback-only, self-hosted endpoints regardless of what this benchmark measures.
- **Not identical to the production path for the LLM.** The app's `LocalVllmAuditAdapter`
  targets a vLLM OpenAI-compatible server. This benchmark uses vLLM's offline `LLM` engine
  only if it is already importable on the image — it never `pip install`s vLLM by default,
  because a fresh vLLM wheel is tightly coupled to a specific CUDA runtime ABI and an
  unpinned install can require a newer CUDA than the image ships (the first run of this
  benchmark hit exactly that: a build needing `libcudart.so.13` on an image without it),
  and pip's resolver can then also replace the image's already-working preinstalled `torch`
  while chasing vLLM's requirements — breaking the `transformers` fallback too, even though
  vLLM itself never loaded. Set `BENCHMARK_TRY_VLLM=1` to opt into an explicit, pinned vLLM
  install instead, once you have separately confirmed it matches this image's CUDA version.
  The fallback path (`transformers` + 4-bit quantisation) never installs or upgrades `torch`
  either, for the same reason. The output JSON always records which engine actually ran, so
  a vLLM number is never reported as if it were the fallback's.
- **Not a substitute for the golden-set protocol.** It runs on synthetic fixtures generated
  in the script itself (a silent WAV, a short synthetic transcript). It measures throughput
  and output-schema validity, not word-error-rate or rubric agreement.

## Files

- `run_benchmark.py.template` — the script pushed to Kaggle. Checked in as a template with
  `__RUBRIC_B64__` / `__PROMPT_B64__` / `__RUBRIC_HASH__` / `__PROMPT_HASH__` placeholders so
  the exact committed `config/rubric.v1.json` and `prompts/audit.v1.txt` (with their SHA-256)
  travel with the run, preserving lineage instead of drifting from a copy-pasted version.
- `push_and_collect.py` — local orchestrator. Fills the template from the current repo files,
  pushes it as a private Kaggle kernel, polls for completion, and pulls `benchmark_result.json`
  back into `results/<UTC timestamp>/`.
- `kernel-metadata.json.template` — filled with the Kaggle owner slug at push time.
- `requirements.txt` — pinned version of the `kaggle` API client used only by the local
  orchestrator; never installed into the application's own environment.

## Running it

Two things must exist first, in the environment's settings (not in chat):

1. A **`KAGGLE_API_TOKEN`** secret — generate one at `kaggle.com/settings` → API → "Generate
   New Token", and add it as an environment variable/secret. The token is a credential;
   never paste it into a prompt.
2. **`api.kaggle.com`** (and `www.kaggle.com`) allowed in the environment's outbound network
   settings — the Kaggle API host, not the interactive notebook-proxy host.

Then, with your public Kaggle username (safe to share — it's on your profile URL):

```bash
pip install -r benchmarks/kaggle_gpu_benchmark/requirements.txt
python benchmarks/kaggle_gpu_benchmark/push_and_collect.py --owner <your-kaggle-username>
```

The script prints the kernel URL, polls until it finishes or fails, and writes the pulled
result and raw log under `benchmarks/kaggle_gpu_benchmark/results/`. Enable a GPU on the
kernel from Kaggle's session settings if the script's own attempt to request one via the
metadata is not honoured by your account tier.

## After it runs

Read `results/<timestamp>/benchmark_result.json`. Report the numbers in
`docs/operations-and-evaluation.md` labelled explicitly as **"measured on Kaggle
[GPU model], not target production hardware"** — per `AGENTS.md`, a measurement from
different hardware is not a production capacity claim, and must not be presented as one.
