import unittest

from app.telecmi import TeleCMIProtocolError, TeleCMIStream


class TeleCMIStreamTests(unittest.TestCase):
    def test_accepts_bounded_binary_as_unknown_speaker(self):
        stream = TeleCMIStream(started_at=10)
        frame = stream.accept(b"\x00\xff", now=10.1)
        self.assertEqual(frame.payload, b"\x00\xff")
        self.assertEqual(frame.speaker, "UNKNOWN")

    def test_rejects_nonbinary_empty_oversized_and_expired_frames(self):
        stream = TeleCMIStream(started_at=10)
        for message in ("not binary", b"", b"x" * (stream.MAX_FRAME_BYTES + 1)):
            with self.subTest(message=type(message).__name__, size=len(message)):
                with self.assertRaises(TeleCMIProtocolError):
                    stream.accept(message, now=10.1)
        with self.assertRaises(TeleCMIProtocolError):
            stream.accept(b"audio", now=10 + stream.MAX_SESSION_SECONDS + 0.1)

    def test_stream_byte_limit_is_cumulative(self):
        stream = TeleCMIStream(started_at=0)
        stream.MAX_STREAM_BYTES = 4
        stream.accept(b"1234", now=1)
        with self.assertRaises(TeleCMIProtocolError):
            stream.accept(b"5", now=2)

    def test_rejects_invalid_monotonic_time(self):
        stream = TeleCMIStream(started_at=1)
        for now in ("2", float("nan"), float("inf"), 0):
            with self.subTest(now=now):
                with self.assertRaises(TeleCMIProtocolError):
                    stream.accept(b"audio", now=now)


if __name__ == "__main__":
    unittest.main()
