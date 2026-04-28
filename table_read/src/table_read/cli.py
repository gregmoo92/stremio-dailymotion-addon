"""Command-line entrypoint.

Subcommands:
  cost     Print an estimated provider bill without making any API calls.
  run      Full pipeline (analyze -> cast -> direct -> render).
  assemble Stitch existing rendered lines into a final MP3 (no re-render).
  recast   Override one character's voice and trigger a re-render on next run.
  lock     Freeze the current casting so future runs reuse it.
  parse    Run only the regex parse stage (debug).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from . import budget as budget_mod
from . import orchestrator
from . import playback as playback_mod
from . import screenplay as sp_module
from .models import (
    DirectionTrack,
    Manifest,
    Screenplay,
)


def _here_default_catalog() -> Path:
    """Find voice_catalog.json next to the package, walking up if needed."""
    p = Path(__file__).resolve()
    # Try packaged layout: <root>/voice_catalog.json
    for candidate in (
        p.parent.parent.parent / "voice_catalog.json",
        p.parent.parent / "voice_catalog.json",
        Path.cwd() / "voice_catalog.json",
    ):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "voice_catalog.json not found.  Pass --catalog explicitly."
    )


def _load_dotenv_if_present() -> None:
    here = Path.cwd()
    for candidate in (here / ".env", Path(__file__).resolve().parent.parent.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            return
    load_dotenv()  # fall back to default search


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tableread")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_cost = sub.add_parser("cost", help="Estimate provider bill, no API calls.")
    sp_cost.add_argument("script", type=Path)

    sp_run = sub.add_parser("run", help="Full pipeline + final MP3 assembly.")
    sp_run.add_argument("script", type=Path)
    sp_run.add_argument("--out", type=Path, default=Path("output"))
    sp_run.add_argument("--catalog", type=Path, default=None)
    sp_run.add_argument("--max-cost", type=float, default=None)
    sp_run.add_argument(
        "--skip-repair", action="store_true",
        help="Use regex-only beats; skip the LLM repair pass.",
    )
    sp_run.add_argument(
        "--no-assemble", action="store_true",
        help="Skip final ffmpeg MP3 assembly.",
    )
    sp_run.add_argument(
        "--force-recast", action="store_true",
        help="Recompute casting even if casting.locked.json is present.",
    )

    sp_asm = sub.add_parser("assemble", help="Re-stitch existing lines into MP3.")
    sp_asm.add_argument("--out", type=Path, default=Path("output"))
    sp_asm.add_argument("--bitrate", default="192k")

    sp_recast = sub.add_parser("recast", help="Set CHAR=voice_id override.")
    sp_recast.add_argument("--out", type=Path, default=Path("output"))
    sp_recast.add_argument(
        "assignments", nargs="+",
        help="One or more CHARACTER=voice_id pairs.",
    )

    sp_lock = sub.add_parser("lock", help="Freeze current casting.")
    sp_lock.add_argument("--out", type=Path, default=Path("output"))

    sp_parse = sub.add_parser("parse", help="Run regex parser, dump beats.")
    sp_parse.add_argument("script", type=Path)

    return p.parse_args()


def main() -> int:
    args = _parse_args()
    _setup_logging(args.verbose)
    _load_dotenv_if_present()

    if args.cmd == "cost":
        return _cmd_cost(args)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "assemble":
        return _cmd_assemble(args)
    if args.cmd == "recast":
        return _cmd_recast(args)
    if args.cmd == "lock":
        return _cmd_lock(args)
    if args.cmd == "parse":
        return _cmd_parse(args)
    print(f"unknown command: {args.cmd}", file=sys.stderr)
    return 2


def _cmd_cost(args: argparse.Namespace) -> int:
    text = args.script.read_text(encoding="utf-8")
    sp = sp_module.parse_regex(text, title=args.script.stem)
    est = budget_mod.estimate(sp)
    print(est.pretty())
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set.  Did you copy .env.example to .env?",
              file=sys.stderr)
        return 1
    if not os.environ.get("ELEVENLABS_API_KEY"):
        print("ELEVENLABS_API_KEY not set.  Did you copy .env.example to .env?",
              file=sys.stderr)
        return 1

    text = args.script.read_text(encoding="utf-8")
    sp = sp_module.parse_regex(text, title=args.script.stem)
    est = budget_mod.estimate(sp)
    print(est.pretty())
    if args.max_cost is not None and est.total_usd > args.max_cost:
        print(
            f"Estimated cost ${est.total_usd:0.3f} exceeds --max-cost "
            f"${args.max_cost:0.3f}; aborting.",
            file=sys.stderr,
        )
        return 1

    catalog_path = args.catalog or _here_default_catalog()
    options = orchestrator.RunOptions(
        voice_catalog_path=catalog_path,
        out_dir=args.out,
        skip_repair=args.skip_repair,
        force_recast=args.force_recast,
    )
    result = asyncio.run(
        orchestrator.run(script_path=args.script, options=options)
    )
    print(f"\nRendered {result['n_lines']} lines for run {result['run_uuid']}.")
    print(f"Manifest: {result['manifest_path']}")

    if not args.no_assemble:
        mp3 = _assemble_run(args.out, result["run_uuid"])
        print(f"Table read MP3: {mp3}")
    return 0


def _assemble_run(out_dir: Path, run_uuid: str) -> Path:
    artifacts = orchestrator.Artifacts.at(out_dir)
    sp = Screenplay.model_validate_json(artifacts.beats.read_text(encoding="utf-8"))
    direction_tracks = [
        DirectionTrack.model_validate(d)
        for d in json.loads(artifacts.direction.read_text(encoding="utf-8"))
    ]
    manifest = Manifest.model_validate_json(
        artifacts.manifest.read_text(encoding="utf-8")
    )
    output_path = artifacts.runs_dir / run_uuid / "table_read.mp3"
    return playback_mod.assemble(
        screenplay=sp,
        direction_tracks=direction_tracks,
        manifest=manifest,
        out_dir=artifacts.out_dir,
        output_path=output_path,
    )


def _cmd_assemble(args: argparse.Namespace) -> int:
    artifacts = orchestrator.Artifacts.at(args.out)
    if not artifacts.manifest.exists():
        print(f"No manifest at {artifacts.manifest}; nothing to assemble.",
              file=sys.stderr)
        return 1
    new_run_uuid = str(uuid.uuid4())
    out_path = _assemble_run(args.out, new_run_uuid)
    print(f"Assembled: {out_path}")
    return 0


def _cmd_recast(args: argparse.Namespace) -> int:
    for assignment in args.assignments:
        if "=" not in assignment:
            print(f"Bad assignment '{assignment}', expected CHARACTER=voice_id",
                  file=sys.stderr)
            return 2
        name, voice_id = assignment.split("=", 1)
        path = orchestrator.write_recast(args.out, character=name.strip(), voice_id=voice_id.strip())
    print(f"Wrote overrides to {path}")
    print("Re-run with `tableread run <script>` to re-render the affected lines.")
    return 0


def _cmd_lock(args: argparse.Namespace) -> int:
    path = orchestrator.lock_casting(args.out)
    print(f"Locked casting to {path}")
    return 0


def _cmd_parse(args: argparse.Namespace) -> int:
    text = args.script.read_text(encoding="utf-8")
    sp = sp_module.parse_regex(text, title=args.script.stem)
    print(sp.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
