# Self-hosted models and open-source resources

Status: candidate stack for evaluation, not installed or benchmarked. This document records the user's requirement that call audio, transcripts and audit prompts be processed by self-hosted models. Keep model files on project-controlled infrastructure and run inference without outbound model API calls. Download and verify model artifacts during controlled provisioning, then pin exact revisions and checksums. Evaluate each candidate on consented, representative contact-centre data before promotion.

## Default candidates by job

| Job | Candidate and deployment | What must be proven |
|---|---|---|
| Post-call speech recognition | [Whisper](https://github.com/openai/whisper) weights served locally through [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | English/Indian-English word error rate, timestamps, long-call memory and GPU throughput. Faster-whisper is an implementation; verify both its license and the exact weight artifact. |
| Live speech recognition | Same local faster-whisper worker with bounded overlapping windows, [Silero VAD](https://github.com/snakers4/silero-vad) and a stability/finalisation policy | Time to stable segment, correction rate, silence/overlap handling and concurrent call capacity. Whisper is not natively streaming; windowed output is provisional until reconciled. |
| Speaker attribution | Prefer separate agent/customer telephony channels. For mono calls, evaluate locally run [pyannote.audio](https://github.com/pyannote/pyannote-audio) with its [Community-1 model](https://huggingface.co/pyannote/speaker-diarization-community-1). | Speaker error rate on noisy/overlapping calls and unknown-role rate. The Community-1 model card lists CC-BY-4.0 and requires accepting access conditions; verify the license/terms for the actual pinned artifact. A speaker cluster is not automatically an AGENT/CUSTOMER label. |
| Voice activity | Silero VAD, local ONNX or PyTorch execution | Speech/hold/silence recall on telephony audio; do not assume VAD alone identifies IVR or hold music. |
| PII detection | [Presidio](https://github.com/data-privacy-stack/presidio) with local NLP recognisers plus tested structured patterns | Misses on names, addresses, account identifiers, Indian phone numbers and payment data; redact before audit inference, storage and UI. |
| Sentiment | A pinned [Transformers](https://huggingface.co/docs/transformers) checkpoint selected through a contact-centre evaluation | Three-class calibration by language/accent, uncertainty and latency. A Twitter-trained checkpoint is not assumed to generalise to calls. |
| Audit scoring | [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) served locally by [vLLM](https://github.com/vllm-project/vllm), initial benchmark candidate | Rubric agreement, citation validity, abstention, prompt-injection resistance, context length, GPU memory and post-call latency. The model card lists Apache-2.0; quality has not been established for this use case. Application validation remains mandatory even with constrained output. |
| Semantic policy matching | [sentence-transformers](https://www.sbert.net/) with a pinned, separately licensed local embedding checkpoint | Only add after a real paraphrase rule fails deterministic matching and labelled threshold calibration is available. Similarity is advisory, never a final policy ruling. |

Use [LibROSA](https://librosa.org/) only when a required audio analysis is absent from the codec/VAD tooling. Do not load it into the live path for general preprocessing. Keep deterministic compliance rules independent of the LLM.

Whisper's repository states its code and weights are MIT licensed. [Whisper license](https://github.com/openai/whisper/blob/main/LICENSE) The Qwen3-8B model card identifies Apache-2.0. [Qwen3-8B model card](https://huggingface.co/Qwen/Qwen3-8B) Pyannote Community-1 documents offline use and its access conditions. [Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1) Licenses of software, model weights, training derivatives and datasets must be checked separately at the exact revisions chosen.

## Deployment contract

```text
Telephony/upload -> private audio storage -> local VAD/STT/diarization
-> local PII redaction -> deterministic policy rules
-> local sentiment + local audit LLM -> PostgreSQL -> scoped UI
```

Inference services bind to a private network. The application may reach identity, storage and the approved telephony endpoint, but STT/LLM requests stay on that network. Use local model paths in staging and production; disable automatic Hub downloads in inference processes. Provisioning records artifact URL, revision, SHA-256, license/notice, acceptance record, runtime version, quantisation and benchmark result. Never place Hub access tokens in request logs or model configuration committed to Git.

Schedule GPU work by stage: live STT has priority, post-call STT is queued, and audit generation is queued behind real-time work or uses separate GPU capacity. Keep bounded queues and visible delay states. Benchmark GPU memory, real-time factor, p50/p95/p99 latency, peak power, utilisation, model load time and cost per completed call on actual hardware. Quantisation or model swaps create new evaluated versions; do not silently change a running audit model.

The proposed one-second live alert target begins when a final utterance arrives. Windowed Whisper may take longer to produce that final; measure audio-to-final latency separately and set an achievable service target after the pilot benchmark. If the selected local model cannot meet the required live accuracy and latency at target concurrency, select a different self-hosted open-weight model or revise the requirement with the product owner before live release. Do not route calls to a hosted AI service as an implicit fallback.

## Resources from the article and their status

| Resource | Planned use |
|---|---|
| [Pyannote.audio](https://github.com/pyannote/pyannote-audio), [Silero VAD](https://github.com/snakers4/silero-vad), [LibROSA](https://librosa.org/) | Conditional speaker analysis, VAD and optional audio features as described above |
| [Transformers](https://huggingface.co/docs/transformers), [Presidio](https://github.com/data-privacy-stack/presidio), [sentence-transformers](https://www.sbert.net/) | Local sentiment and redaction; embeddings only if a measured rule needs them |
| [Apache Kafka](https://kafka.apache.org/), [ClickHouse](https://clickhouse.com/), [Temporal](https://temporal.io/) | Future infrastructure if database queue throughput, indexed reporting or workflow recovery fails measured targets |
| [LangSmith](https://smith.langchain.com/), [Weights & Biases](https://wandb.ai/), [PromptFlow](https://microsoft.github.io/promptflow/) | References only. Use local versioned prompts, restricted logs and evaluation artifacts first; confirm data residency before considering hosted observability. |

Relevant reading: [Whisper: Robust Speech Recognition via Large-Scale Weak Supervision](https://arxiv.org/abs/2212.04356), [pyannote.audio 2.1 speaker diarization pipeline](https://www.isca-archive.org/interspeech_2023/bredin23_interspeech.html), and [Constitutional AI: Harmlessness from AI Feedback](https://arxiv.org/abs/2212.08073). The article's “COLM: Using Language Models for Automated Call Centre Monitoring” citation is too ambiguous to identify reliably; do not cite it as a specific paper until a title, authors and publication link are verified. Constitutional AI is background on model alignment, not a validation method for this call-audit rubric.
