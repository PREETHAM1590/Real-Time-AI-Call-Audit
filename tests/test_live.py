"""Provider-independent boundary checks for the live sentiment heuristic."""

import math
import unittest

from app.sentiment import sentiment_alert_times, sentiment_drop


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


if __name__ == "__main__":
    unittest.main()
