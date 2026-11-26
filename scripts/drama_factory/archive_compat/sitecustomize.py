"""Resolve frozen V4 manifest reads for an archived Python runner and its children."""
import io
import os
from pathlib import Path
import sys


def use_archived_runtime():
    archived = os.getenv('VISION_MEDIA_ARCHIVED_H3_RUNTIME')
    live = os.getenv('VISION_MEDIA_LIVE_H3_RUNTIME')
    if not archived or not live or not sys.argv:
        return
    scripts = Path(archived).parents[2] / 'scripts'
    if Path(sys.argv[0]).resolve().parent != scripts:
        return

    original_open = io.open

    def open_runtime(file, mode='r', *args, **kwargs):
        # Existing runners hash and load this exact pathname from their batch.
        if mode == 'rb' and not isinstance(file, int) and os.fspath(file) == live:
            file = archived
        return original_open(file, mode, *args, **kwargs)

    io.open = open_runtime


use_archived_runtime()
