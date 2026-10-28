import asyncio

import aiohttp
from aiohttp import web

from seamer.simkit.clock import RealClock, VirtualClock
from seamer.simkit.kits.discovery import SsdpResponder, SsdpTarget, parse_ssdp
from seamer.simkit.kits.http import HttpServer, LongPoll, command_middleware
from seamer.simkit.knobs import KnobSet
from seamer.simkit.recorder import Recorder


async def test_longpoll_etag_semantics():
    clock = VirtualClock()
    rec = Recorder(clock=clock)
    lp = LongPoll(clock, "status", rec.bind("lp"), min_interval_s=1.0)
    assert await lp.wait(None, 10) is True  # no etag: answer now
    assert await lp.wait("stale", 10) is True
    etag = lp.etag
    waiter = asyncio.ensure_future(lp.wait(etag, 30, client="a"))
    await clock.advance(5)
    assert not waiter.done()
    lp.bump()
    await clock.advance(0)
    assert await waiter is True
    timed = asyncio.ensure_future(lp.wait(lp.etag, 30, client="a"))
    await clock.advance(31)
    assert await timed is False
    await lp.wait(lp.etag, 0, client="a")  # immediate re-poll: rate violation
    names = rec.timeline().names()
    assert "longpoll_rate_violation" in names
    assert "longpoll_wake" in names


async def test_command_middleware_records_and_injects():
    clock = RealClock()
    rec = Recorder(clock=clock)
    ks = KnobSet("example", {"latency_s": 0.1}, rec=rec.bind("dev"))

    async def hello(_req):
        return web.Response(text="OK")

    app = web.Application(middlewares=[command_middleware(rec.bind("dev"), ks, clock)])
    app.router.add_get("/httpapi.asp", hello)
    srv = HttpServer(app)
    await srv.start()
    try:
        async with aiohttp.ClientSession() as s:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            async with s.get(f"{srv.base_url}/httpapi.asp?command=getStatusEx") as r:
                assert await r.text() == "OK"
            assert loop.time() - t0 >= 0.09
            ks.set_live("http_error", {"p": 1.0, "status": 502})
            async with s.get(f"{srv.base_url}/httpapi.asp") as r:
                assert r.status == 502
            ks.set_live("http_error", False)
            ks.set_live("offline", True)
            async with s.get(f"{srv.base_url}/httpapi.asp") as r:
                assert r.status == 503
    finally:
        await srv.stop()
    tl = rec.timeline()
    cmd = tl.first(kind="cmd")
    assert cmd.data["query"] == {"command": "getStatusEx"}
    assert tl.first(kind="resp").data["body"] == "OK"
    assert tl.first(kind="resp", fault="http_error").data["status"] == 502
    assert tl.first(kind="resp", fault="offline") is not None


async def test_ssdp_responder_unicast_reply():
    target = SsdpTarget(
        "http://127.0.0.1:1/desc.xml", "uuid:abc", ["urn:schemas-upnp-org:device:MediaRenderer:1"]
    )
    resp = SsdpResponder([target])
    await resp.start()
    loop = asyncio.get_running_loop()
    got: asyncio.Future = loop.create_future()

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not got.done():
                got.set_result(data)

    tr, _ = await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
    msg = (
        b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
        b"MX: 1\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
    )
    tr.sendto(msg, ("127.0.0.1", resp.port))
    data = await asyncio.wait_for(got, 2)
    first, headers = parse_ssdp(data)
    assert first == "HTTP/1.1 200 OK"
    assert headers["USN"] == "uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1"
    tr.close()
    await resp.stop()
