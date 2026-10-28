import pytest_asyncio

from seamer.simkit.kits.http import HttpServer
from seamer.simkit.media import MediaServer


@pytest_asyncio.fixture
async def media_srv(media):
    """A MediaServer over the session media dir; yields (MediaServer, base_url)."""
    ms = MediaServer(media.root)
    srv = HttpServer(ms.app())
    await srv.start()
    try:
        yield ms, srv.base_url
    finally:
        await srv.stop()
