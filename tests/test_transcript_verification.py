import unittest

from app.transcript_verification import (
    VerificationInputError,
    align_words,
    compare_transcript,
    normalise_words,
    word_error_rate,
)


class NormaliseWordsTests(unittest.TestCase):
    def test_lowercases_and_strips_edge_punctuation_only(self):
        self.assertEqual(normalise_words("Hello, world! Don't stop."), ["hello", "world", "don't", "stop"])

    def test_empty_and_whitespace_only_text_gives_no_words(self):
        self.assertEqual(normalise_words(""), [])
        self.assertEqual(normalise_words("   \n\t  "), [])

    def test_rejects_non_string_input(self):
        with self.assertRaises(VerificationInputError):
            normalise_words(None)


class AlignWordsTests(unittest.TestCase):
    def test_identical_sequences_are_all_matches(self):
        ops = align_words(["one", "two", "three"], ["one", "two", "three"])
        self.assertEqual([op["op"] for op in ops], ["match", "match", "match"])

    def test_missing_word_is_a_deletion(self):
        ops = align_words(["please", "hold", "the", "line"], ["please", "the", "line"])
        self.assertEqual([op["op"] for op in ops], ["match", "delete", "match", "match"])
        self.assertEqual(ops[1]["reference"], "hold")
        self.assertIsNone(ops[1]["hypothesis"])

    def test_extra_word_is_an_insertion(self):
        ops = align_words(["thank", "you"], ["thank", "you", "sir"])
        self.assertEqual([op["op"] for op in ops], ["match", "match", "insert"])
        self.assertIsNone(ops[2]["reference"])
        self.assertEqual(ops[2]["hypothesis"], "sir")

    def test_mismatched_word_is_a_substitution(self):
        ops = align_words(["account", "closed"], ["account", "close"])
        self.assertEqual([op["op"] for op in ops], ["match", "substitute"])
        self.assertEqual((ops[1]["reference"], ops[1]["hypothesis"]), ("closed", "close"))

    def test_empty_reference_is_all_insertions_and_empty_hypothesis_is_all_deletions(self):
        self.assertEqual([op["op"] for op in align_words([], ["a", "b"])], ["insert", "insert"])
        self.assertEqual([op["op"] for op in align_words(["a", "b"], [])], ["delete", "delete"])
        self.assertEqual(align_words([], []), [])

    def test_bounded_input_size_is_enforced(self):
        with self.assertRaises(VerificationInputError):
            align_words(["word"] * 20_001, [])


class WordErrorRateTests(unittest.TestCase):
    def test_exact_match_has_zero_error_rate(self):
        result = word_error_rate("Thank you for calling support.", "Thank you for calling support.")
        self.assertEqual(result["wer"], 0.0)
        self.assertEqual((result["missed_words"], result["invented_words"], result["misheard_pairs"]), ([], [], []))

    def test_a_missed_word_is_reported_and_counted(self):
        # STT dropped "immediately" - a genuine capture gap, not a normalisation artifact.
        result = word_error_rate("We will escalate this immediately today.", "We will escalate this today.")
        self.assertEqual(result["deletions"], 1)
        self.assertEqual(result["missed_words"], ["immediately"])
        self.assertAlmostEqual(result["wer"], 1 / 6)

    def test_an_invented_word_is_reported_and_counted(self):
        result = word_error_rate("I understand your concern.", "I understand your urgent concern.")
        self.assertEqual(result["insertions"], 1)
        self.assertEqual(result["invented_words"], ["urgent"])

    def test_a_misheard_word_is_reported_as_a_pair(self):
        result = word_error_rate("Your balance is fifty dollars.", "Your balance is sixty dollars.")
        self.assertEqual(result["substitutions"], 1)
        self.assertEqual(result["misheard_pairs"], [("fifty", "sixty")])

    def test_empty_reference_gives_undefined_wer_not_a_crash(self):
        result = word_error_rate("", "hello")
        self.assertIsNone(result["wer"])
        self.assertEqual(result["insertions"], 1)

    def test_punctuation_and_case_differences_alone_do_not_count_as_errors(self):
        result = word_error_rate("Is this the Billing Department?", "is this the billing department")
        self.assertEqual(result["wer"], 0.0)


class CompareTranscriptTests(unittest.TestCase):
    def _utterances(self, *rows):
        return [{"id": row[0], "start_ms": row[1], "end_ms": row[2], "text": row[3]} for row in rows]

    def test_missed_word_is_attributed_to_the_following_utterance(self):
        reference = "Thank you for calling. We will refund the full amount today."
        hypothesis = self._utterances(
            ("u1", 0, 2000, "Thank you for calling."),
            ("u2", 2000, 5000, "We will refund the amount today."),  # "full" missing
        )
        result = compare_transcript(reference, hypothesis)
        self.assertEqual(result["deletions"], 1)
        self.assertEqual(result["missed_words"], ["full"])
        per_utterance = {row["id"]: row for row in result["per_utterance"]}
        self.assertEqual(per_utterance["u2"]["missed_words_before"], ["full"])
        self.assertEqual(per_utterance["u1"]["missed_words_before"], [])

    def test_missed_word_after_the_last_utterance_is_reported_separately(self):
        reference = "Goodbye and thank you for calling."
        hypothesis = self._utterances(("u1", 0, 1000, "Goodbye and thank you"))
        result = compare_transcript(reference, hypothesis)
        self.assertEqual(result["missed_words_after_last_utterance"], ["for", "calling"])

    def test_substitution_and_invented_word_are_attributed_to_their_own_utterance(self):
        reference = "Your account number is one two three."
        hypothesis = self._utterances(
            ("u1", 0, 1000, "Your account number is one two four"),  # three -> four
            ("u2", 1000, 2000, "please confirm that"),  # "please" invented relative to reference continuation
        )
        result = compare_transcript(reference, hypothesis)
        per_utterance = {row["id"]: row for row in result["per_utterance"]}
        self.assertEqual(per_utterance["u1"]["substitutions"], [("three", "four")])

    def test_rejects_malformed_utterance_entries(self):
        with self.assertRaises(Exception):
            compare_transcript("hello", [{"id": "u1", "text": "hello"}])  # missing start_ms/end_ms

    def test_per_utterance_covers_every_supplied_utterance_even_with_no_errors(self):
        reference = "All good here."
        hypothesis = self._utterances(("u1", 0, 500, "All good here."))
        result = compare_transcript(reference, hypothesis)
        self.assertEqual(len(result["per_utterance"]), 1)
        self.assertEqual(result["per_utterance"][0]["missed_words_before"], [])


if __name__ == "__main__":
    unittest.main()
