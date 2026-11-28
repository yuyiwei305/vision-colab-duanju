#!/usr/bin/env python3
"""Project commands and local review of generated footage."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
KEY = "skills/short-drama/scripts/project_tool.py"
SOURCE = ROOT / "ref/drama-skills" / KEY


def load_engine():
    lock = json.loads((ROOT / "drama-skills.lock.json").read_text())
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != lock["files_sha256"][KEY]:
        raise RuntimeError("Pinned project tool changed; review aspect-ratio compatibility")
    spec = importlib.util.spec_from_file_location("drama_project_engine", SOURCE)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    engine.FORMAT_POINTERS += ("/format/aspect_ratio",)
    write = engine._write_authority_value

    def write_aspect(project, tokens, value):
        if tokens == ["format", "aspect_ratio"] and (
            not isinstance(value, str) or value not in {"9:16", "16:9", "1:1", "4:3", "3:4", "21:9"}
        ):
            raise ValueError("Unsupported project aspect ratio")
        return write(project, tokens, value)

    engine._write_authority_value = write_aspect
    return engine


if __name__ == "__main__":
    engine = load_engine()
    if sys.argv[1:2] == ["review-motion"]:
        from drama_review_motion import main

        raise SystemExit(main(engine, sys.argv[2:]))
    if sys.argv[1:] in (["--help"], ["-h"]):
        parser = engine.build_parser()
        parser.epilog = "review-motion  按分镜顺序查看指定生成段、相邻段和已取回的原片。"
        parser.print_help()
    else:
        raise SystemExit(engine.main())
