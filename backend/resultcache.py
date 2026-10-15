"""Remembers the answer to a slow, display-only question for a little while.

At a few hundred thousand leads, a dashboard total is a pass over every row and can take seconds on a slow computer, and
the screens ask for it again every few seconds. This keeps the last answer when (and only when) working it out was slow,
and throws it away the moment anyone changes anything through the app. Small databases are never affected: a question
that takes less than SLOW seconds is not remembered, so those screens stay exactly as live as before.

It is only ever used for numbers that are shown, never for anything that decides what is sent to whom.
"""
import threading
import time

SLOW = 0.15   # seconds: a question that takes longer than this is worth remembering


class ResultCache:
    def __init__(self, slow=None, clock=time.monotonic):
        self._lock = threading.Lock()
        self._items = {}      # key -> (when, value, epoch)
        self._epoch = 0       # bumped by every change made through the app
        self._slow, self._clock = (SLOW if slow is None else slow), clock

    def bump(self) -> None:
        """Something was changed: everything remembered so far may be out of date."""
        with self._lock:
            self._epoch += 1

    def get(self, key: str, ttl: float, compute):
        now = self._clock()
        with self._lock:
            hit = self._items.get(key)
            epoch = self._epoch
            if hit and hit[2] == epoch and now - hit[0] < ttl:
                return hit[1]
        started = self._clock()
        value = compute()
        if self._clock() - started >= self._slow:
            with self._lock:
                # stored under the epoch from BEFORE it was worked out, so a change made meanwhile makes it stale at once
                self._items[key] = (self._clock(), value, epoch)
        else:
            with self._lock:
                self._items.pop(key, None)
        return value
