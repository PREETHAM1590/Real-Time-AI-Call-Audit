import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.contracts import Utterance
from app.sentiment_adapter import (
    LocalSentimentAdapter,
    SentimentUnavailable,
    SentimentValidationError,
    compute_call_sentiment,
)


def _verified_model_directory(tmp_path: str) -> tuple[str, str]:
    from app.artifacts import directory_sha256

    root = Path(tmp_path)
    (root / "config.json").write_text("{}")
    return str(root), directory_sha256(root)


def _utterance(id_, role, start_ms, end_ms, text, *, is_final=True):
    return Utterance(id=id_, role=role, start_ms=start_ms, end_ms=end_ms, text_redacted=text, is_final=is_final)


class LocalSentimentAdapterTests(unittest.TestCase):
    def test_from_environment_returns_none_when_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(LocalSentimentAdapter.from_environment(classify=lambda text: []))

    def test_positive_and_negative_labels_produce_signed_score_and_top_probability(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            adapter = LocalSentimentAdapter(
                artifact_path=path, artifact_sha256=digest,
                classify=lambda text: [{"label": "positive", "score": 0.8}, {"label": "neutral", "score": 0.15}, {"label": "negative", "score": 0.05}],
            )
            signed_score, top_class_probability = adapter.classify_text("great service")
        self.assertAlmostEqual(signed_score, 0.75)
        self.assertEqual(top_class_probability, 0.8)

    def test_malformed_model_output_fails_closed_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            for bad_output in ([], [{"label": "positive", "score": 1.5}], [{"label": "positive", "score": 0.9}, {"label": "positive", "score": 0.1}], "not a list"):
                with self.subTest(bad_output=bad_output):
                    adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=lambda _text, out=bad_output: out)
                    with self.assertRaises(SentimentValidationError):
                        adapter.classify_text("some text")

    def test_inference_exception_becomes_unavailable_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            def broken_classify(_text):
                raise RuntimeError("model process died")
            adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=broken_classify)
            with self.assertRaises(SentimentUnavailable):
                adapter.classify_text("some text")

    def test_bounded_input_length_rejected_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            calls = []
            adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=lambda text: calls.append(text) or [{"label": "neutral", "score": 1.0}])
            with self.assertRaises(SentimentValidationError):
                adapter.classify_text("x" * 4001)
            with self.assertRaises(SentimentValidationError):
                adapter.classify_text("   ")
            self.assertEqual(calls, [])

    def test_warm_runs_exactly_one_bounded_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            calls = []
            adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=lambda text: calls.append(text) or [{"label": "neutral", "score": 1.0}])
            adapter.warm()
            adapter.warm()
        self.assertEqual(len(calls), 1)

    def test_unverified_artifact_path_fails_at_construction(self):
        with self.assertRaises(ValueError):
            LocalSentimentAdapter(artifact_path="/nonexistent/path", artifact_sha256="0" * 64, classify=lambda text: [])


class ComputeCallSentimentTests(unittest.TestCase):
    def test_no_adapter_returns_unavailable_and_never_blocks(self):
        result = compute_call_sentiment([_utterance("u1", "CUSTOMER", 0, 1000, "hello")], adapter=None)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["alert_offsets_ms"], [])

    def test_only_final_customer_utterances_are_sent_to_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            sent = []
            adapter = LocalSentimentAdapter(
                artifact_path=path, artifact_sha256=digest,
                classify=lambda text: sent.append(text) or [{"label": "neutral", "score": 1.0}],
            )
            utterances = [
                _utterance("u1", "AGENT", 0, 1000, "agent line"),
                _utterance("u2", "CUSTOMER", 1000, 2000, "customer line", is_final=False),
                _utterance("u3", "UNKNOWN", 2000, 3000, "mono line"),
                _utterance("u4", "IVR", 3000, 4000, "ivr line"),
                _utterance("u5", "CUSTOMER", 4000, 5000, "final customer line"),
            ]
            result = compute_call_sentiment(utterances, adapter)
        self.assertEqual(sent, ["final customer line"])
        self.assertEqual(result["status"], "OK")
        self.assertEqual([signal["id"] for signal in result["signals"]], ["u5"])

    def test_per_utterance_inference_failure_is_excluded_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)

            def flaky_classify(text):
                if "fails" in text:
                    raise RuntimeError("boom")
                return [{"label": "positive", "score": 0.6}, {"label": "negative", "score": 0.4}]

            adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=flaky_classify)
            utterances = [
                _utterance("u1", "CUSTOMER", 0, 1000, "this fails"),
                _utterance("u2", "CUSTOMER", 1000, 2000, "this works"),
            ]
            result = compute_call_sentiment(utterances, adapter)
        self.assertEqual(result["status"], "OK")
        self.assertEqual([signal["id"] for signal in result["signals"]], ["u2"])
        self.assertEqual(result["failed_utterance_count"], 1)

    def test_all_failures_report_unavailable_not_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=lambda text: (_ for _ in ()).throw(RuntimeError("down")))
            result = compute_call_sentiment([_utterance("u1", "CUSTOMER", 0, 1000, "hi")], adapter)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["signals"], [])

    def test_alert_offsets_delegate_to_pure_sentiment_decision_logic(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)

            def scripted_classify(text):
                # Three highly-positive utterances, then three sharply negative ones in the next window.
                if text.startswith("prior"):
                    return [{"label": "positive", "score": 0.95}, {"label": "negative", "score": 0.05}]
                return [{"label": "negative", "score": 0.95}, {"label": "positive", "score": 0.05}]

            adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=scripted_classify)
            utterances = [_utterance(f"p{i}", "CUSTOMER", i * 1000, i * 1000 + 500, f"prior {i}") for i in range(3)]
            utterances += [_utterance(f"c{i}", "CUSTOMER", 30_000 + i * 1000, 30_000 + i * 1000 + 500, f"current {i}") for i in range(3)]
            result = compute_call_sentiment(utterances, adapter)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["alert_offsets_ms"], [60_000])


if __name__ == "__main__":
    unittest.main()
