import base64
import io
import unittest
import wave

from app.exotel_adapter import (
    build_wav,
    integration_credentials,
    make_audio_references,
    verify_integration_secret,
)


class ExotelAdapterTests(unittest.TestCase):
    def test_generated_secret_is_verified_without_persisting_plaintext(self):
        username, password, salt, verifier = integration_credentials()
        self.assertTrue(username)
        self.assertNotEqual(password.encode(), salt)
        self.assertNotEqual(password.encode(), verifier)
        self.assertTrue(verify_integration_secret(password, salt, verifier))
        self.assertFalse(verify_integration_secret(password + "x", salt, verifier))

    def test_recording_is_wav_and_reference_is_deterministic_and_scoped(self):
        pcm = b"\x01\x00" * 800
        audio = build_wav(pcm)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (8000, 1, 2))
            self.assertEqual(wav.readframes(800), pcm)
        first = make_audio_references("org-a", "acct-a", "call-a")
        self.assertEqual(first, make_audio_references("org-a", "acct-a", "call-a"))
        self.assertNotEqual(first, make_audio_references("org-b", "acct-a", "call-a"))


if __name__ == "__main__":
    unittest.main()
