"""Provider-independent boundary checks for the live sentiment heuristic."""

import math
import unittest

from app.sentiment import sentiment_drop


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


if __name__ == "__main__":
    unittest.main()
