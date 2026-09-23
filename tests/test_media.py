"""Provider-independent final transcript buffering checks."""

import unittest

from app.media import FinalBuffer


class FinalBufferTests(unittest.TestCase):
    def test_partials_replace_and_final_is_stable(self):
        buffer = FinalBuffer()
        buffer.apply("s1", "may", False)
        buffer.apply("s1", "may be recorded", False)
        self.assertEqual(buffer.partials, {"s1": "may be recorded"})
        self.assertEqual(buffer.final_text(), "")

        buffer.apply("s1", "may be recorded", True)
        self.assertNotIn("s1", buffer.partials)
        buffer.apply("s1", "duplicate final", True)
        buffer.apply("s1", "late provisional text", False)
        self.assertEqual(buffer.final_text(), "may be recorded")
        self.assertNotIn("s1", buffer.partials)

    def test_final_text_preserves_caller_order(self):
        buffer = FinalBuffer()
        buffer.apply("s1", "first", True)
        buffer.apply("s2", "second", True)
        self.assertEqual(buffer.final_text(), "first second")


if __name__ == "__main__":
    unittest.main()
