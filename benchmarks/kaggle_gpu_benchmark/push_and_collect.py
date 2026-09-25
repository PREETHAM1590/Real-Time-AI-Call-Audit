"""Fill the benchmark template from this repo's pinned rubric/prompt, push it as a
private Kaggle kernel, poll until it finishes, and pull the result back.

Needs KAGGLE_API_TOKEN in the environment (never pass it as an argument or paste it into
chat) and the Kaggle API host allowed outbound. See README.md in this directory.

Usage:
    python benchmarks/kaggle_gpu_benchmark/push_and_collect.py --owner <kaggle-username>
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
BUILD_DIR = HERE / ".build"
POLL_SECONDS = 30
MAX_WAIT_SECONDS = 6 * 3600


def encode_pinned_file(relative_path: str) -> tuple[str, str]:
    raw = (REPO_ROOT / relative_path).read_bytes()
    return base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest()


def render_template(path: Path, substitutions: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="Your Kaggle username (public; the id shown in your profile URL).")
    args = parser.parse_args()

    if not os.environ.get("KAGGLE_API_TOKEN"):
        sys.exit("KAGGLE_API_TOKEN is not set. Add it as an environment secret first; see README.md. Refusing to continue.")

    rubric_b64, rubric_sha256 = encode_pinned_file("config/rubric.v1.json")
    prompt_b64, prompt_sha256 = encode_pinned_file("prompts/audit.v1.txt")

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)

    (BUILD_DIR / "run_benchmark.py").write_text(
        render_template(HERE / "run_benchmark.py.template", {
            "__RUBRIC_B64__": rubric_b64, "__PROMPT_B64__": prompt_b64,
            "__RUBRIC_HASH__": rubric_sha256, "__PROMPT_HASH__": prompt_sha256,
        }),
        encoding="utf-8",
    )
    (BUILD_DIR / "kernel-metadata.json").write_text(
        render_template(HERE / "kernel-metadata.json.template", {"__OWNER__": args.owner}),
        encoding="utf-8",
    )

    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()

    kernel_id = f"{args.owner}/call-audit-gpu-benchmark"
    print(f"Pushing kernel {kernel_id} ...")
    api.kernels_push(str(BUILD_DIR))
    print(f"Pushed. View at https://www.kaggle.com/code/{kernel_id}")

    print("Polling for completion (this runs on Kaggle's queue; a GPU slot may not be immediate) ...")
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    terminal_statuses = {"COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED", "CANCEL_REQUESTED"}
    status_name = None
    while time.monotonic() < deadline:
        response = api.kernels_status(kernel_id)
        raw_status = getattr(response, "status", None) or (response.get("status") if isinstance(response, dict) else None)
        # The Kaggle SDK returns a KernelWorkerStatus enum here, not a plain string; normalise
        # via .name so the terminal-state comparison below actually matches instead of looping
        # until MAX_WAIT_SECONDS.
        status_name = getattr(raw_status, "name", str(raw_status)).upper()
        print(f"  status: {raw_status}")
        if status_name in terminal_statuses:
            break
        time.sleep(POLL_SECONDS)
    else:
        sys.exit(f"Timed out after {MAX_WAIT_SECONDS}s waiting for the kernel to finish; check {kernel_id} on Kaggle directly.")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output_dir = HERE / "results" / stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    api.kernels_output(kernel_id, path=str(output_dir))
    print(f"Pulled kernel output to {output_dir}")

    if status_name != "COMPLETE":
        sys.exit(f"Kernel finished with status {status_name!r}; inspect {output_dir} and the Kaggle log before trusting any partial result.")

    result_path = output_dir / "benchmark_result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        print(json.dumps(result, indent=2))
        print(f"\nResult saved at {result_path}")
        print("Transcribe these numbers into docs/operations-and-evaluation.md, labelled as measured on Kaggle hardware, not target production hardware.")
    else:
        print(f"No benchmark_result.json found under {output_dir}; check the pulled log for what actually ran.")


if __name__ == "__main__":
    main()
