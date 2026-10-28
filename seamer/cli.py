"""``seamer`` command line.

seamer families
seamer profiles <family>
seamer knobs <family> [--profile P] [--json]
seamer run <family> [--profile P] [--port N] [--host H] [--seed S]
                    [--knob name=value ...] [--id ID] [--timeline FILE] [--echo]
seamer media <dir> [--tracks N] [--seconds S] [--format wav|flac|mp3]
seamer timeline <file.jsonl> [--src PREFIX] [--kind K] [--name PREFIX]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import tomllib
from pathlib import Path
from typing import Any

from .families import build_device, discover, errors, get_family
from .simkit.knobs import KnobSet, list_faults
from .simkit.media import MediaFactory
from .simkit.recorder import Recorder, Timeline


def parse_knob(arg: str) -> tuple[str, Any]:
    """``name=value``; the value is parsed as TOML (``true``, ``1.5``, ``{p=0.2}``) or kept as
    a string."""
    if "=" not in arg:
        raise argparse.ArgumentTypeError(f"--knob needs name=value, got {arg!r}")
    name, raw = arg.split("=", 1)
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    return name.strip(), value


def _cmd_families(_args: argparse.Namespace) -> int:
    for name, fam in sorted(discover().items()):
        alias = f" [alias: {', '.join(fam.aliases)}]" if fam.aliases else ""
        print(f"{name:<12} {fam.description}  (default profile: {fam.default_profile}){alias}")
    for mod, err in errors().items():
        print(f"! {mod}: failed to import: {err}", file=sys.stderr)
    return 0


def _cmd_profiles(args: argparse.Namespace) -> int:
    fam = get_family(args.family)
    for name in fam.profiles():
        prof = fam.load_profile(name)
        print(f"{name:<24} {prof.description}")
    print("\nfault bundles (stack with +):")
    for name in list_faults():
        print(f"  {name}")
    return 0


def _cmd_knobs(args: argparse.Namespace) -> int:
    fam = get_family(args.family)
    prof = fam.load_profile(args.profile)
    ks = KnobSet.from_profile(prof)
    rows = ks.describe()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for r in rows:
        mark = "*" if r["value"] != r["default"] else " "
        quirks = ",".join(r["quirks"])
        print(
            f"{mark} {r['family']:<8} {r['name']:<28} {r['type']:<6} "
            f"{json.dumps(r['value'], default=str):<16} [{r['confidence']}] {quirks}"
        )
        if args.verbose and r["doc"]:
            print(f"      {r['doc']}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    overrides = dict(args.knob or [])
    rec = Recorder(args.timeline, echo=args.echo)
    dev = build_device(
        args.family,
        args.profile,
        recorder=rec,
        seed=args.seed,
        device_id=args.id,
        host=args.host,
        port=args.port if args.port is not None else get_family(args.family).default_port,
        overrides=overrides,
    )
    await dev.start()
    print(
        f"seamer: {args.family} '{dev.profile.name}' listening on {dev.base_url} "
        f"(control: {dev.base_url}/_sim/state)",
        flush=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        if args.duration:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), args.duration)
        else:
            await stop.wait()
    finally:
        await dev.stop()
        rec.close()
    return 0


def _cmd_media(args: argparse.Namespace) -> int:
    mf = MediaFactory(Path(args.dir))
    for t in mf.album(args.tracks, args.seconds, args.format):
        print(f"{t.path}  track {t.index}  {t.seconds:g}s  {t.tone_hz:g} Hz")
    return 0


def _cmd_timeline(args: argparse.Namespace) -> int:
    tl = Timeline.load(args.file)
    match: dict[str, Any] = {}
    if args.src:
        match["src"] = args.src + "*"
    if args.kind:
        match["kind"] = args.kind
    if args.name:
        match["name"] = args.name + "*"
    for ev in tl.where(**match):
        print(ev.short())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seamer", description="Streamer Seamer device simulators")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("families", help="list device families").set_defaults(fn=_cmd_families)

    sp = sub.add_parser("profiles", help="list a family's profiles")
    sp.add_argument("family")
    sp.set_defaults(fn=_cmd_profiles)

    sp = sub.add_parser("knobs", help="list knobs and their values for a profile")
    sp.add_argument("family")
    sp.add_argument("--profile", default=None)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(fn=_cmd_knobs)

    sp = sub.add_parser("run", help="run one simulated device until interrupted")
    sp.add_argument("family")
    sp.add_argument("--profile", default=None, help="profile spec, e.g. basic+faults/slow_commands")
    sp.add_argument("--port", type=int, default=None, help="0 = any free port")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--id", default=None, help="device id (default <family>1)")
    sp.add_argument("--knob", action="append", type=parse_knob, metavar="NAME=VALUE")
    sp.add_argument("--timeline", default=None, help="write the JSONL timeline here")
    sp.add_argument("--echo", action="store_true", help="print events as they happen")
    sp.add_argument("--duration", type=float, default=None, help="exit after N seconds")
    sp.set_defaults(fn=None)

    sp = sub.add_parser("media", help="generate tone test tracks")
    sp.add_argument("dir")
    sp.add_argument("--tracks", type=int, default=3)
    sp.add_argument("--seconds", type=float, default=10.0)
    sp.add_argument("--format", default="wav")
    sp.set_defaults(fn=_cmd_media)

    sp = sub.add_parser("timeline", help="print (and filter) a JSONL timeline")
    sp.add_argument("file")
    sp.add_argument("--src")
    sp.add_argument("--kind")
    sp.add_argument("--name")
    sp.set_defaults(fn=_cmd_timeline)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_run(args))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
