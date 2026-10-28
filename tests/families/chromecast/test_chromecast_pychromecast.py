"""Oracle: the real sender library (pychromecast, which MA uses) drives the sim."""

import asyncio
import time
import uuid

import pytest

from seamer.simkit.scenario import Scene

pychromecast = pytest.importorskip("pychromecast")
models = pytest.importorskip("pychromecast.models")


async def test_pychromecast_play_pause_seek_volume_finish(scene: Scene):
    dev = await scene.device("chromecast/nest_mini")
    await scene.media_server()
    url = scene.media_url(scene.media.track(1, 2.0).path)
    seen: dict = {}

    def drive() -> None:
        info = models.CastInfo(
            {models.HostServiceInfo("127.0.0.1", dev.cast.port)},
            uuid.UUID(dev.uuid_hex),
            dev.model_name,
            dev.friendly_name,
            "127.0.0.1",
            dev.cast.port,
            "audio",
            "Google Inc.",
        )
        cc = pychromecast.get_chromecast_from_cast_info(info, None, tries=1, timeout=5)
        try:
            cc.wait(timeout=5)
            seen["volume0"] = cc.status.volume_level
            mc = cc.media_controller
            mc.play_media(url, "audio/wav", title="Tone 1")
            mc.block_until_active(timeout=5)
            deadline = time.monotonic() + 5
            while mc.status.player_state != "PLAYING" and time.monotonic() < deadline:
                time.sleep(0.05)
            seen["playing"] = mc.status.player_state
            seen["title"] = mc.status.title
            mc.pause()
            time.sleep(0.3)
            seen["paused"] = mc.status.player_state
            mc.seek(1.0)
            time.sleep(0.3)
            seen["after_seek"] = (mc.status.player_state, mc.status.current_time)
            mc.play()
            cc.set_volume(0.55)
            time.sleep(0.3)
            seen["volume"] = cc.status.volume_level
            deadline = time.monotonic() + 6
            while mc.status.player_state != "IDLE" and time.monotonic() < deadline:
                time.sleep(0.05)
            seen["end"] = (mc.status.player_state, mc.status.idle_reason)
        finally:
            cc.disconnect(timeout=3)

    await asyncio.to_thread(drive)
    assert seen["volume0"] == pytest.approx(0.3)
    assert seen["playing"] == "PLAYING"
    assert seen["title"] == "Tone 1"
    assert seen["paused"] == "PAUSED"
    assert seen["after_seek"] == ("PLAYING", 1.0)  # pychromecast sends resumeState PLAYBACK_START
    assert seen["volume"] == pytest.approx(0.55)
    assert seen["end"] == ("IDLE", "FINISHED")
    assert dev.sink.bytes_read > 0
