"""DlnaSink: the simkit HttpSink plus the mid-stream Range re-request (X5, low confidence).

Some renderers (Samsung TVs, seen over Cast in bug_crossmatch X5) read the start of a
stream, drop the connection and fetch the rest with ``Range: bytes=N-``. A server that
answers anything but 206 (MA's stream server always answers 200 with
``Accept-Ranges: none``) ends playback early.
"""

from __future__ import annotations

from ...simkit.sink.decoder import Decoder, pick_decoder
from ...simkit.sink.http_sink import HttpSink, SinkRejected


class DlnaSink(HttpSink):
    async def _read_once(self, url: str, probe: bool) -> str:
        split_at = int(self.knobs.get("range_rerequest_after_bytes"))
        if split_at <= 0:
            return await super()._read_once(url, probe)
        k = self.knobs
        session = await self._session_get()
        if probe and k.get("probe") != "none":
            await self._probe(session, url, k.get("probe"))
        realtime = k.get("read_mode") == "realtime"
        first = {"Range": "bytes=0-"} if k.get("require_range") else {}
        decoder: Decoder | None = None
        try:
            for part, headers in ((0, first), (1, {"Range": f"bytes={split_at}-"})):
                async with session.get(url, headers=headers) as resp:
                    ctype = resp.headers.get("Content-Type")
                    self._emit(
                        "opened",
                        status=resp.status,
                        content_type=ctype,
                        chunked="chunked" in resp.headers.get("Transfer-Encoding", "").lower(),
                        content_length=resp.content_length,
                        part=part,
                        range=headers.get("Range"),
                    )
                    if resp.status >= 400:
                        raise SinkRejected(f"HTTP {resp.status}")
                    if part == 1 and resp.status != 206:
                        self.rec.knob(
                            "fired",
                            knob="range_rerequest_after_bytes",
                            quirks=["X5", "ma:#6775"],
                            confidence="low",
                            status=resp.status,
                            offset=split_at,
                        )
                        raise SinkRejected(f"range re-request refused: HTTP {resp.status}")
                    if decoder is None:
                        decoder = pick_decoder(ctype, k.get("decoder"), k.get("assumed_kbps"))
                        self.decoder_name = decoder.name
                        self._decoder = decoder
                        await decoder.start(self._on_audio)
                    while True:
                        while realtime and self.buffer.full:
                            await self.clock.sleep(self.tick_s)
                        want = self.read_bytes
                        if part == 0:
                            want = min(want, split_at - self.bytes_read)
                            if want <= 0:
                                self._emit("range_split", bytes=self.bytes_read)
                                break
                        data = await resp.content.read(want)
                        if not data:
                            break
                        self.bytes_read += len(data)
                        await decoder.feed(data)
                    if part == 0 and self.bytes_read < split_at:
                        break  # whole body was shorter than the split point
            assert decoder is not None
            await decoder.finish()
            self._flush_pcm()
        except BaseException:
            if decoder is not None:
                await decoder.abort()
            raise
        self._emit(
            "eof",
            bytes=self.bytes_read,
            received_s=round(self.buffer.received_s, 3),
            unplayed_s=round(self.buffer.fill_s, 3),
        )
        return "eof"
