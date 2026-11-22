"""Derived, read-only minute arrays; original market-data files stay untouched."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


class DetailArrayCache:
    def __init__(self, root, revision):
        self.root = Path(root) / revision
        self.root.mkdir(parents=True, exist_ok=True)

    def paths(self, key):
        token = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        return tuple(self.root / (token + suffix) for suffix in (".timestamps.npy", ".ohlc.npy", ".complete"))

    def load(self, key):
        timestamps, ohlc, complete = self.paths(key)
        if not complete.exists():
            return None
        return np.load(timestamps, mmap_mode="r", allow_pickle=False), np.load(ohlc, mmap_mode="r", allow_pickle=False)

    def save(self, key, entry):
        timestamps, ohlc, complete = self.paths(key)
        for path, array in zip((timestamps, ohlc), entry):
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("wb") as stream:
                np.save(stream, array, allow_pickle=False)
            temporary.replace(path)
        # Publish only after both exact arrays are fully written.
        temporary = complete.with_suffix(".tmp")
        temporary.write_text("Normalized by Freqtrade; includes its existing missing-bar treatment.\n")
        temporary.replace(complete)
        return self.load(key)
