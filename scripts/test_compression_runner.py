import unittest

from scripts.compression_runner import audio_bitrate_kbps, video_bitrate_kbps


class CompressionMathTests(unittest.TestCase):
    def test_audio_bitrate_is_bounded(self):
        self.assertEqual(audio_bitrate_kbps(19, 3600), 38)
        self.assertGreaterEqual(audio_bitrate_kbps(19, 1), 24)
        self.assertLessEqual(audio_bitrate_kbps(19, 1), 160)

    def test_video_bitrate_is_bounded(self):
        self.assertGreaterEqual(video_bitrate_kbps(19, 3600), 60)
        self.assertLessEqual(video_bitrate_kbps(45, 30), 3500)


if __name__ == "__main__":
    unittest.main()
