import pytest

from seamer.simkit.media import (
    estimate_tone_hz,
    ffmpeg_bin,
    pcm_tone,
    tone_for_track,
    track_for_tone,
)
from seamer.simkit.sink.decoder import (
    BitrateEstimate,
    FfmpegDecoder,
    PcmDecoder,
    pick_decoder,
)


def test_tone_mapping_roundtrip():
    for n in range(1, 15):
        hz = estimate_tone_hz(pcm_tone(0.1, tone_for_track(n)))
        assert track_for_tone(hz) == n
    assert track_for_tone(None) is None
    assert track_for_tone(estimate_tone_hz(bytes(17640))) is None  # silence


async def _decode(dec, data, chunk):
    got = {"s": 0.0, "pcm": 0}

    def cb(secs, pcm):
        got["s"] += secs
        got["pcm"] += len(pcm or b"")

    await dec.start(cb)
    for i in range(0, len(data), chunk):
        await dec.feed(data[i : i + chunk])
    await dec.finish()
    return got


@pytest.mark.parametrize("chunk", [1, 7, 37, 4096])
async def test_pcm_decoder_wav_exact_any_chunking(media, chunk):
    t = media.track(1, 0.5)
    got = await _decode(PcmDecoder(), t.path.read_bytes(), chunk)
    assert got["s"] == pytest.approx(0.5, abs=1e-4)


async def test_pcm_decoder_streaming_header_size_zero(media):
    data = bytearray(media.track(2, 0.3).path.read_bytes())
    data[40:44] = b"\xff\xff\xff\xff"  # unknown data size, as live streams send
    got = await _decode(PcmDecoder(), bytes(data), 1000)
    assert got["s"] == pytest.approx(0.3, abs=1e-4)


async def test_bitrate_estimate():
    got = await _decode(BitrateEstimate(128), bytes(16000), 1000)
    assert got["s"] == pytest.approx(1.0)


@pytest.mark.skipif(ffmpeg_bin() is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("fmt", ["flac", "mp3"])
async def test_ffmpeg_decoder(media, fmt):
    t = media.track(3, 2.0, fmt=fmt)
    got = await _decode(FfmpegDecoder(), t.path.read_bytes(), 4096)
    # mp3 adds encoder padding; flac is exact
    assert got["s"] == pytest.approx(2.0, abs=0.06 if fmt == "mp3" else 0.001)


def test_pick_decoder():
    assert pick_decoder("audio/wav").name == "pcm"
    assert pick_decoder("audio/L16;rate=48000;channels=2").rate == 48000
    assert pick_decoder("audio/flac", "bitrate").name == "bitrate"
    if ffmpeg_bin():
        assert pick_decoder("audio/flac").name == "ffmpeg"
