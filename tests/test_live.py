"""Provider-independent boundary checks for the live sentiment heuristic."""

import math
import unittest

from app.sentiment import customer_speech_trend, sentiment_alert_times, sentiment_drop


class SentimentDropTests(unittest.TestCase):
    def test_requires_three_samples_in_each_adjacent_window(self):
        self.assertFalse(sentiment_drop([0.5], [-0.5]))
        self.assertFalse(sentiment_drop([0.5, 0.5], [-0.5, -0.5, -0.5]))
        self.assertFalse(sentiment_drop([0.5, 0.5, 0.5], [-0.5, -0.5]))

    def test_detects_threshold_drop_and_rejects_smaller_drop(self):
        self.assertTrue(sentiment_drop([0.4, 0.4, 0.4], [0.0, 0.0, 0.0]))
        self.assertFalse(sentiment_drop([0.399, 0.399, 0.399], [0.0, 0.0, 0.0]))
        self.assertFalse(sentiment_drop([0.2, 0.2, 0.2], [0.1, 0.1, 0.1]))

    def test_requires_finite_signed_sentiment_scores(self):
        for previous, current in (
            ("not-a-window", [0.0, 0.0, 0.0]),
            ([True, 0.0, 0.0], [0.0, 0.0, 0.0]),
            ([math.nan, 0.0, 0.0], [0.0, 0.0, 0.0]),
            ([math.inf, 0.0, 0.0], [0.0, 0.0, 0.0]),
            ([10**5000, 0.0, 0.0], [0.0, 0.0, 0.0]),
            ([1.01, 0.0, 0.0], [0.0, 0.0, 0.0]),
            ([0.0, 0.0, 0.0], [-1.01, 0.0, 0.0]),
        ):
            with self.subTest(previous=previous, current=current), self.assertRaises(ValueError):
                sentiment_drop(previous, current)


class SentimentAlertTests(unittest.TestCase):
    @staticmethod
    def signal(start_ms, score, *, signal_id=None, role="CUSTOMER", is_final=True, probability=0.8, end_ms=None):
        return {
            "id": f"signal-{start_ms}" if signal_id is None else signal_id,
            "role": role,
            "start_ms": start_ms,
            "end_ms": start_ms if end_ms is None else end_ms,
            "is_final": is_final,
            "signed_score": score,
            "top_class_probability": probability,
        }

    def test_uses_only_final_customer_signals_in_adjacent_windows(self):
        signals = [
            *(self.signal(i * 100, 0.8) for i in range(2)),
            self.signal(200, 0.8, is_final=False),
            self.signal(300, 0.8, role="AGENT"),
            *(self.signal(30_000 + i * 100, 0.0) for i in range(3)),
        ]
        self.assertEqual(sentiment_alert_times(signals), [])

        signals = [self.signal(i * 100, 0.8) for i in range(3)]
        signals.extend(self.signal(30_000 + i * 100, 0.0) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [60_000])

    def test_requires_adjacent_windows_and_average_probability_threshold(self):
        signals = [self.signal(i * 100, 0.8) for i in range(3)]
        signals.extend(self.signal(60_000 + i * 100, 0.0) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [])

        signals = [self.signal(i * 100, 0.8, probability=0.69) for i in range(3)]
        signals.extend(self.signal(30_000 + i * 100, 0.0, probability=0.69) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [])
        signals = [self.signal(i * 100, 0.8, probability=0.7) for i in range(3)]
        signals.extend(self.signal(30_000 + i * 100, 0.0, probability=0.7) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [60_000])

        signals = [self.signal(i * 100, 0.5, probability=0.7) for i in range(3)]
        signals.extend(self.signal(30_000 + i * 100, 0.1, probability=0.7) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [60_000])

        signals = [self.signal(i * 100, 0.6, probability=0.7) for i in range(3)]
        signals.extend(self.signal(30_000 + i * 100, 0.2, probability=0.7) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [60_000])

        signals = [self.signal(i * 100, 0.6, probability=0.7) for i in range(3)]
        signals.extend(self.signal(30_000 + i * 100, 0.2000000000005, probability=0.7) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [])

        signals = [self.signal(i * 100, 0.8, probability=0.6999999999999) for i in range(3)]
        signals.extend(self.signal(30_000 + i * 100, 0.0, probability=0.6999999999999) for i in range(3))
        self.assertEqual(sentiment_alert_times(signals), [])

    def test_enforces_ninety_second_cooldown(self):
        scores = [0.8, 0.0, 0.8, 0.8, 0.0]
        signals = [self.signal(i * 30_000 + j, scores[i])
                   for i in range(5) for j in range(3)]
        self.assertEqual(sentiment_alert_times(signals), [60_000, 150_000])

    def test_rejects_invalid_eligible_offsets_and_scores(self):
        for changes in (
            {"start_ms": True},
            {"end_ms": 1.5},
            {"end_ms": -1},
            {"signed_score": math.nan},
            {"top_class_probability": 1.01},
        ):
            with self.subTest(changes=changes):
                signal = self.signal(0, 0.5)
                signal.update(changes)
                with self.assertRaises(ValueError):
                    sentiment_alert_times([signal])

    def test_rejects_missing_or_duplicate_utterance_ids(self):
        signal = self.signal(0, 0.5)
        signal.pop("id")
        with self.assertRaises(ValueError):
            sentiment_alert_times([signal])

        repeated = self.signal(0, 0.5)
        signals = [repeated, repeated.copy(), self.signal(100, 0.5), self.signal(200, 0.5)]
        signals.extend(self.signal(30_000 + i * 100, 0.0) for i in range(3))
        with self.assertRaises(ValueError):
            sentiment_alert_times(signals)

        conflicting = [self.signal(0, 0.5, signal_id="same"), self.signal(100, 0.4, signal_id="same")]
        with self.assertRaises(ValueError):
            sentiment_alert_times(conflicting)

    def test_rejects_non_mapping_signals(self):
        with self.assertRaises(ValueError):
            sentiment_alert_times([None])

    def test_rejects_non_string_roles(self):
        for role in ([], {}):
            with self.subTest(role=role), self.assertRaises(ValueError):
                sentiment_alert_times([self.signal(0, 0.5, role=role)])


class CustomerSpeechTrendTests(unittest.TestCase):
    @staticmethod
    def signal(start_ms, end_ms, score, *, signal_id=None, role="CUSTOMER", is_final=True, probability=0.8):
        return {
            "id": f"signal-{start_ms}" if signal_id is None else signal_id,
            "role": role, "start_ms": start_ms, "end_ms": end_ms, "is_final": is_final,
            "signed_score": score, "top_class_probability": probability,
        }

    def test_missing_customer_speech_gives_null_summary(self):
        self.assertIsNone(customer_speech_trend([]))
        self.assertIsNone(customer_speech_trend([self.signal(0, 1000, 0.5, role="AGENT")]))
        self.assertIsNone(customer_speech_trend([self.signal(0, 1000, 0.5, is_final=False)]))

    def test_single_short_utterance_is_both_first_and_last_window(self):
        result = customer_speech_trend([self.signal(0, 1000, 0.6)])
        self.assertEqual(result["first_60s_mean_signed_score"], 0.6)
        self.assertEqual(result["last_60s_mean_signed_score"], 0.6)
        self.assertEqual(result["first_60s_customer_speech_ms"], 1000)
        self.assertEqual(result["trend_delta"], 0.0)

    def test_duration_weighting_favours_the_longer_utterance(self):
        # 1s at +1.0 and 9s at -1.0 in the same (only) window: weighted mean is -0.8, not the -0.0 unweighted average.
        result = customer_speech_trend([self.signal(0, 1000, 1.0), self.signal(1000, 10_000, -1.0)])
        self.assertAlmostEqual(result["first_60s_mean_signed_score"], -0.8)

    def test_improving_trend_is_a_positive_delta_between_first_and_last_60_seconds(self):
        # Negative for the first 60s, positive for a separate later 60s: distinct, non-overlapping windows.
        signals = [self.signal(i * 10_000, i * 10_000 + 10_000, -0.8) for i in range(6)]
        signals += [self.signal(120_000 + i * 10_000, 120_000 + i * 10_000 + 10_000, 0.8) for i in range(6)]
        result = customer_speech_trend(signals)
        self.assertAlmostEqual(result["first_60s_mean_signed_score"], -0.8)
        self.assertAlmostEqual(result["last_60s_mean_signed_score"], 0.8)
        self.assertAlmostEqual(result["trend_delta"], 1.6)

    def test_short_total_speech_windows_overlap_rather_than_erroring(self):
        # Only 20s of total customer speech: well under the 60s window on both sides.
        signals = [self.signal(0, 10_000, 0.5), self.signal(10_000, 20_000, -0.5)]
        result = customer_speech_trend(signals)
        self.assertEqual(result["first_60s_customer_speech_ms"], 20_000)
        self.assertEqual(result["last_60s_customer_speech_ms"], 20_000)
        self.assertEqual(result["first_60s_mean_signed_score"], result["last_60s_mean_signed_score"])

    def test_non_customer_and_partial_signals_are_excluded_from_the_window(self):
        signals = [self.signal(0, 1000, 0.5, role="AGENT"), self.signal(1000, 2000, -1.0, is_final=False),
                   self.signal(2000, 3000, 0.3)]
        result = customer_speech_trend(signals)
        self.assertEqual(result["first_60s_customer_speech_ms"], 1000)
        self.assertEqual(result["first_60s_mean_signed_score"], 0.3)


if __name__ == "__main__":
    unittest.main()
