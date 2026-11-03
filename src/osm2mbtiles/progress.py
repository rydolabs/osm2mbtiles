"""Small stdlib-only progress reporter for Mapnik and PostGIS tile exporters.

Tile percentages come from actual completed XYZ tiles. Unknown-length imports
must use elapsed-only heartbeats, never invented percentages. Lines are durable
and remain readable through WSL, redirected logs and GitHub Actions.
"""
from __future__ import annotations

import sys
import threading
import time


def duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class TileProgress:
    def __init__(self, stage: str, total: int, *, completed: int = 0,
                 metatiles: int | None = None, stream=None, clock=None,
                 update_seconds: float = 5, heartbeat_seconds: float = 20):
        if total < 0 or not 0 <= completed <= total:
            raise ValueError("Invalid progress total/completed tile count")
        self.stage = stage
        self.total = total
        self.completed = completed
        self.resumed = completed
        self.metatiles = metatiles
        self.stream = stream if stream is not None else sys.stdout
        self.clock = clock or time.monotonic
        self.update_seconds = update_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.started = self.clock()
        self.last_emitted = self.started
        self.last_percent = (100 * completed / total) if total else 100
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False

    def _write(self, line: str):
        print(f"[{self.stage}] {line}", file=self.stream, flush=True)

    def __enter__(self):
        meta = f" | metatiles={self.metatiles:,}" if self.metatiles is not None else ""
        with self._lock:
            self._write(f"START | total={self.total:,} tiles{meta} | "
                        f"already-complete={self.resumed:,} | "
                        f"{self.last_percent:.1f}%")
        if self.heartbeat_seconds > 0:
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()
        return self

    def _heartbeat(self):
        # A render or SQL call can block between progress checkpoints. Distinguish
        # "still working" from real progress, and never guess an import fraction.
        while not self._stop.wait(self.heartbeat_seconds):
            with self._lock:
                if self._closed:
                    return
                now = self.clock()
                if now - self.last_emitted >= self.heartbeat_seconds:
                    self._write(f"WORKING | {self.completed:,}/{self.total:,} tiles "
                                f"| elapsed={duration(now - self.started)} "
                                "(current tile/query still running)")
                    self.last_emitted = now

    def advance(self, tiles: int):
        if tiles < 0:
            raise ValueError("Progress cannot move backward")
        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot advance closed progress")
            if self.completed + tiles > self.total:
                raise ValueError("More tiles completed than scheduled")
            self.completed += tiles
            now = self.clock()
            pct = 100 * self.completed / self.total if self.total else 100
            if (self.completed == self.total or
                    pct >= self.last_percent + 5 or
                    now - self.last_emitted >= self.update_seconds):
                processed = self.completed - self.resumed
                elapsed = max(0.001, now - self.started)
                rate = processed / elapsed
                outstanding = self.total - self.completed
                eta = duration(outstanding / rate) if rate else "unknown"
                self._write(f"{pct:5.1f}% | {self.completed:,}/{self.total:,} tiles "
                            f"| {rate:,.1f} tiles/s | ETA {eta} "
                            f"| elapsed {duration(elapsed)}")
                self.last_percent = pct
                self.last_emitted = now

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            self._closed = True
            elapsed = duration(self.clock() - self.started)
            if exc_type is None and self.completed == self.total:
                self._write(f"COMPLETE | {self.completed:,}/{self.total:,} tiles "
                            f"| elapsed {elapsed}")
            elif exc_type is None:
                # A deliberately skipped/unwritten task must not be called 100%.
                self._write(f"INCOMPLETE | {self.completed:,}/{self.total:,} tiles "
                            f"| elapsed {elapsed}")
            else:
                self._write(f"FAILED | {self.completed:,}/{self.total:,} tiles "
                            f"| elapsed {elapsed}")
        return False
