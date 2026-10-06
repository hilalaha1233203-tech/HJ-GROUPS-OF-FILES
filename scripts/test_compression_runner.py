import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.compression_runner import (
    audio_bitrate_kbps,
    video_bitrate_kbps,
    compress_audio,
    compress_video,
    optimize_document,
    build_input_media,
)


class CompressionMathTests(unittest.TestCase):
    def test_audio_bitrate_is_bounded(self):
        self.assertEqual(audio_bitrate_kbps(19, 3600), 38)
        self.assertGreaterEqual(audio_bitrate_kbps(19, 1), 24)
        self.assertLessEqual(audio_bitrate_kbps(19, 1), 160)

    def test_video_bitrate_is_bounded(self):
        self.assertGreaterEqual(video_bitrate_kbps(19, 3600), 60)
        self.assertLessEqual(video_bitrate_kbps(45, 30), 3500)


class CompressionToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("ffmpeg is not installed")
        cls.tmp = tempfile.TemporaryDirectory(prefix="hj-compression-tests-")
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    def test_audio_compression_reaches_target_and_is_smaller(self):
        src = self.dir / "input.wav"
        out = self.dir / "output.m4a"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                "-c:a", "pcm_s16le", str(src),
            ],
            check=True,
        )
        original = src.stat().st_size
        output = compress_audio(str(src), str(out), 0.5, 30)
        self.assertTrue(out.exists())
        self.assertLessEqual(output, original)
        self.assertLessEqual(output, 0.5 * 1024 * 1024)

    def test_video_compression_reaches_target_and_is_smaller(self):
        src = self.dir / "input.mp4"
        out = self.dir / "output.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2M",
                "-c:a", "aac", "-b:a", "128k",
                str(src),
            ],
            check=True,
        )
        original = src.stat().st_size
        output = compress_video(str(src), str(out), 1.0, 20, 1280, 720)
        self.assertTrue(out.exists())
        self.assertLess(output, original)
        self.assertLessEqual(output, 1.0 * 1024 * 1024)

    def test_zip_document_optimization_never_grows_on_repetitive_content(self):
        import zipfile

        src = self.dir / "input.zip"
        out = self.dir / "output.zip"
        payload = b"HJ GROUPS " * 10000
        with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("data.txt", payload)
        original = src.stat().st_size
        output = optimize_document(str(src), str(out), "input.zip")
        self.assertLess(output, original)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.read("data.txt"), payload)

    def test_input_media_constructors_preserve_caption_entities(self):
        source = SimpleNamespace(
            caption="HJ TEST",
            caption_entities=[],
            audio=SimpleNamespace(duration=12, performer="HJ", title="Test"),
            video=None,
            document=None,
        )
        info = {
            "kind": "audio",
            "name": "test.m4a",
            "duration": 12,
        }
        out = str(self.dir / "constructor-test.m4a")
        Path(out).touch()
        media = build_input_media(info, out, source)
        self.assertEqual(getattr(media, "caption", None), "HJ TEST")
        self.assertEqual(getattr(media, "caption_entities", None), [])


if __name__ == "__main__":
    unittest.main()
