from __future__ import annotations

import threading
from collections import deque


class _BoundedQueue:
    def __init__(self, capacity):
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self.capacity = capacity
        self._dq = deque()
        self._lock = threading.Lock()
        self._not_full = threading.Condition(self._lock)
        self._not_empty = threading.Condition(self._lock)

    def enqueue(self, item, block=False, timeout=None):
        with self._not_full:
            if len(self._dq) >= self.capacity:
                if not block:
                    return False
                if not self._not_full.wait_for(
                    lambda: len(self._dq) < self.capacity, timeout=timeout
                ):
                    return False
            self._dq.append(item)
            self._not_empty.notify()
            return True

    def dequeue(self, block=False, timeout=None):
        with self._not_empty:
            if not self._dq:
                if not block:
                    return None
                if not self._not_empty.wait_for(lambda: bool(self._dq), timeout=timeout):
                    return None
            item = self._dq.popleft()
            self._not_full.notify()
            return item

    def size(self):
        with self._lock:
            return len(self._dq)


class SPSCQueue(_BoundedQueue):
    pass


class MPMCQueue(_BoundedQueue):
    pass


class MPSCQueue(_BoundedQueue):
    pass


class LossyQueue:
    def __init__(self, capacity):
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self.capacity = capacity
        self._dq = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def enqueue(self, item, **_):
        with self._lock:
            self._dq.append(item)
        return True

    def dequeue(self, **_):
        with self._lock:
            return self._dq.popleft() if self._dq else None

    def size(self):
        with self._lock:
            return len(self._dq)


class BatchSPSCQueue:
    def __init__(self, capacity):
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self.capacity = capacity
        self._dq = deque()
        self._lock = threading.Lock()

    def enqueue(self, item, **_):
        with self._lock:
            if len(self._dq) >= self.capacity:
                return False
            self._dq.append(item)
            return True

    def dequeue(self, **_):
        with self._lock:
            if not self._dq:
                return []
            batch = list(self._dq)
            self._dq.clear()
            return batch

    def size(self):
        with self._lock:
            return len(self._dq)


_SCENARIO_MAP = {
    "spsc": SPSCQueue,
    "mpmc": MPMCQueue,
    "mpsc": MPSCQueue,
    "lossy": LossyQueue,
    "batch": BatchSPSCQueue,
}
SCENARIOS = list(_SCENARIO_MAP)


def QueueFactory(scenario, capacity=1024):
    cls = _SCENARIO_MAP.get(scenario)
    return cls(capacity=capacity)
