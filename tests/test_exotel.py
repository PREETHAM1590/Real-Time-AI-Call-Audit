"""Synthetic protocol boundary checks for Exotel AgentStream."""

import base64
import json
import unittest

from app.exotel import ExotelProtocolError, ExotelSession


def frame(event, sequence=None, **fields):
    value = {"event": event, **fields}
    if sequence is not None:
        value["sequence_number"] = sequence
    return json.dumps(value)


def start(sequence=1, **changes):
    nested = {
        "stream_sid": "stream-a", "call_sid": "call-a", "account_sid": "acct-a",
        "media_format": {"encoding": "raw", "sample_rate": 8000, "channels": 1, "bit_rate": 16},
    }
    nested.update(changes)
    return frame("start", sequence, stream_sid="stream-a", start=nested)


def media(sequence, chunk=1, timestamp="0", payload=b"\x01\x00"):
    return frame("media", sequence, stream_sid="stream-a", media={
        "chunk": chunk, "timestamp": timestamp,
        "payload": base64.b64encode(payload).decode("ascii"),
    })


class ExotelSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = ExotelSession(account_sid="acct-a", call_sid="call-a", generation=7)

    def open_stream(self):
        self.assertIsNone(self.session.accept(frame("connected"), generation=7))
        self.assertIsNone(self.session.accept(start(), generation=7))

    def test_lifecycle_yields_verified_ordered_frames_and_gap(self):
        self.open_stream()
        item = self.session.accept(media(3, chunk=1, timestamp="12"), generation=7)
        self.assertEqual((item.pcm, item.offset_ms, item.sequence_number, item.chunk), (b"\x01\x00", 12, 3, 1))
        self.assertEqual(item.missing_sequences, (2,))
        self.assertEqual((item.account_sid, item.call_sid, item.stream_sid), ("acct-a", "call-a", "stream-a"))
        self.assertEqual(item.speaker, "UNKNOWN")
        self.session.accept(frame("stop", 4, stream_sid="stream-a", stop={"call_sid": "call-a", "account_sid": "acct-a"}), generation=7)
        with self.assertRaises(ExotelProtocolError):
            self.session.accept(media(5), generation=7)

    def test_requires_connected_before_start_and_bound_identity(self):
        with self.assertRaises(ExotelProtocolError):
            self.session.accept(start(), generation=7)
        self.session.accept(frame("connected"), generation=7)
        with self.assertRaises(ExotelProtocolError):
            self.session.accept(start(account_sid="other"), generation=7)
        with self.assertRaises(ExotelProtocolError):
            self.session.accept(start(), generation=8)

    def test_rejects_duplicate_out_of_order_and_cross_stream_frames(self):
        self.open_stream()
        self.session.accept(media(2), generation=7)
        for bad in (media(2, chunk=2), media(1, chunk=2), frame("media", 3, stream_sid="other", media={"chunk": 2, "timestamp": "1", "payload": "AQI="})):
            with self.assertRaises(ExotelProtocolError):
                self.session.accept(bad, generation=7)

    def test_rejects_malformed_and_unsafe_audio(self):
        self.open_stream()
        for bad in (
            frame("media", 2, stream_sid="stream-a", media={"chunk": 1, "timestamp": "-1", "payload": "AQI="}),
            frame("media", 2, stream_sid="stream-a", media={"chunk": 1, "timestamp": "NaN", "payload": "AQI="}),
            frame("media", 2, stream_sid="stream-a", media={"chunk": 1, "timestamp": "0", "payload": "%%%"}),
            media(2, payload=b"\x00"),
            media(2, payload=b"x" * 100002),
        ):
            with self.assertRaises(ExotelProtocolError):
                self.session.accept(bad, generation=7)

    def test_rejects_unsupported_media_format_and_mismatched_stop(self):
        self.session.accept(frame("connected"), generation=7)
        with self.assertRaises(ExotelProtocolError):
            self.session.accept(start(media_format={"encoding": "wav", "sample_rate": 8000, "channels": 1, "bit_rate": 16}), generation=7)
        self.session = ExotelSession(account_sid="acct-a", call_sid="call-a", generation=7)
        self.open_stream()
        with self.assertRaises(ExotelProtocolError):
            self.session.accept(frame("stop", 2, stream_sid="stream-a", stop={"call_sid": "other", "account_sid": "acct-a"}), generation=7)


if __name__ == "__main__":
    unittest.main()
