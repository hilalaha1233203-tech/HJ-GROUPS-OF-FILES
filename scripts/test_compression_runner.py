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


def _ffprobe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    import json
    return json.loads(result.stdout)


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

    def test_audio_realistic_size_profiles_21_30_50_100_mb(self):
        import math

        sample_rate = 48000
        bytes_per_second = sample_rate * 2
        target = 19 * 1024 * 1024
        for size_mb in (21, 30, 50, 100):
            with self.subTest(size_mb=size_mb):
                src = self.dir / f"input-{size_mb}mb.wav"
                out = self.dir / f"output-{size_mb}mb.m4a"
                expected_duration = math.ceil((size_mb * 1024 * 1024) / bytes_per_second) + 1
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={expected_duration}",
                        "-ac", "1", "-ar", str(sample_rate),
                        "-c:a", "pcm_s16le", str(src),
                    ],
                    check=True,
                )
                original = src.stat().st_size
                self.assertGreater(original, 20 * 1024 * 1024)
                output = compress_audio(str(src), str(out), target, expected_duration)
                self.assertLess(output, original)
                self.assertLessEqual(output, target)
                meta = _ffprobe(str(out))
                streams = [s for s in meta["streams"] if s.get("codec_type") == "audio"]
                self.assertEqual(len(streams), 1)
                self.assertEqual(streams[0]["codec_name"], "aac")
                duration = float(meta["format"]["duration"])
                self.assertLess(abs(duration - expected_duration), 2.0)
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(out), "-f", "null", "-"],
                    check=True,
                )

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

    def test_pdf_optimization_produces_a_real_pdf(self):
        if not shutil.which("gs"):
            self.skipTest("ghostscript is not installed")
        ps = self.dir / "input.ps"
        src = self.dir / "input.pdf"
        out = self.dir / "output.pdf"
        ps.write_text(
            "%!PS
/Helvetica findfont 18 scalefont setfont "
            "72 720 moveto (HJ GROUPS PDF optimisation test) show showpage
",
            encoding="ascii",
        )
        subprocess.run(
            [
                "gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                f"-sOutputFile={src}", str(ps),
            ],
            check=True,
        )
        output = optimize_document(str(src), str(out), "input.pdf")
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes()[:5], b"%PDF-")
        self.assertGreater(output, 0)

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

    def test_video_output_is_h264_aac_and_streaming_compatible(self):
        src = self.dir / "input-streaming.mp4"
        out = self.dir / "output-streaming.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2M",
                "-c:a", "aac", "-b:a", "128k", str(src),
            ],
            check=True,
        )
        output = compress_video(str(src), str(out), 1.0, 20, 1280, 720)
        self.assertLess(output, src.stat().st_size)
        self.assertLessEqual(output, 1.0 * 1024 * 1024)
        meta = _ffprobe(str(out))
        video = next(s for s in meta["streams"] if s.get("codec_type") == "video")
        audio = next(s for s in meta["streams"] if s.get("codec_type") == "audio")
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertGreater(int(video["width"]), 0)
        self.assertGreater(int(video["height"]), 0)
        self.assertLess(abs(float(video.get("duration", 20)) - 20), 2.0)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(out), "-f", "null", "-"],
            check=True,
        )

    def test_zip_document_optimization_never_grows_on_repetitive_content(self):
        import zipfile

        src = self.dir / "input.zip"
        out = self.dir / "output.zip"
        payload = b"HJ GROUPS " * 10000
        with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("data.txt", payload)
        original = src.stat().st_size
        output = optimize_document(str(src), str(out), "input.zip")
        # Optimization must never grow the original. The production runner
        # rejects non-shrinking output before Telegram replacement.
        self.assertLessEqual(output, original)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.read("data.txt"), payload)

    def test_unsupported_document_type_fails_clearly(self):
        src = self.dir / "input.bin"
        out = self.dir / "output.bin"
        src.write_bytes(b"not a supported document optimizer")
        from scripts.compression_runner import optimize_document
        with self.assertRaisesRegex(RuntimeError, "Safe document optimisation"):
            optimize_document(str(src), str(out), "input.bin")

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
