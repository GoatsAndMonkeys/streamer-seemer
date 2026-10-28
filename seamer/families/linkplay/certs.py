"""Throwaway TLS material for the HTTPS API: a device CA, a server cert, a client cert.

Real WiiM units serve a self-signed certificate; Audio Pro MkII units additionally demand
a client certificate (mutual TLS) on port 4443 (lq:Q1, L56). We cannot ship the vendor's
keys, so the sim generates its own CA once per process with the ``openssl`` CLI and signs
a server cert (SAN 127.0.0.1, localhost) and a client cert with it. Clients that do not
verify (the WiiM SDK uses ``verify_ssl=False``) see the same thing as with a real unit;
mTLS clients must present ``client_cert``/``client_key``.
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class CertError(RuntimeError):
    pass


@dataclass(frozen=True)
class CertSet:
    dir: Path
    ca_cert: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path

    def server_context(self, require_client_cert: bool = False) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.server_cert, self.server_key)
        if require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(self.ca_cert)
        return ctx

    def client_context(self, with_cert: bool = True) -> ssl.SSLContext:
        """A client context that skips verification (as the SDK does), optionally mTLS."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if with_cert:
            ctx.load_cert_chain(self.client_cert, self.client_key)
        return ctx


_CACHE: CertSet | None = None


def openssl_bin() -> str | None:
    for cand in ("openssl", "/opt/homebrew/bin/openssl", "/usr/bin/openssl"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def _run(*args: str) -> None:
    exe = openssl_bin()
    if exe is None:
        raise CertError("openssl not found; HTTPS listeners need it to make a test cert")
    proc = subprocess.run([exe, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise CertError(f"openssl {args[0]} failed: {proc.stderr.strip()}")


def certs() -> CertSet:
    """The process-wide test CA and certs (created on first use)."""
    global _CACHE
    if _CACHE is not None and _CACHE.server_cert.exists():
        return _CACHE
    d = Path(tempfile.mkdtemp(prefix="seamer-linkplay-tls-"))
    cs = CertSet(
        dir=d,
        ca_cert=d / "ca.pem",
        server_cert=d / "server.pem",
        server_key=d / "server.key",
        client_cert=d / "client.pem",
        client_key=d / "client.key",
    )
    ca_key = d / "ca.key"
    _run(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-keyout", str(ca_key), "-out", str(cs.ca_cert), "-subj", "/CN=seamer linkplay test CA",
    )  # fmt: skip
    for name, cert, key, ext in (
        ("server", cs.server_cert, cs.server_key, "subjectAltName=IP:127.0.0.1,DNS:localhost"),
        ("client", cs.client_cert, cs.client_key, "extendedKeyUsage=clientAuth"),
    ):
        csr = d / f"{name}.csr"
        extfile = d / f"{name}.ext"
        extfile.write_text(ext + "\n")
        _run(
            "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr),
            "-subj", f"/CN=seamer linkplay {name}",
        )  # fmt: skip
        _run(
            "x509", "-req", "-in", str(csr), "-CA", str(cs.ca_cert), "-CAkey", str(ca_key),
            "-CAcreateserial", "-days", "3650", "-out", str(cert), "-extfile", str(extfile),
        )  # fmt: skip
    _CACHE = cs
    return cs
