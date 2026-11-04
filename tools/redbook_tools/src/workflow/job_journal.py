"""Lazy access to existing job journals with bounded completed-job retention."""
from collections import OrderedDict
from collections.abc import MutableMapping
import heapq
import json
from pathlib import Path
import re


class JobJournal(MutableMapping):
    def __init__(self, directory: Path, *, active_statuses, max_details=8, max_bytes=4 * 1024 * 1024):
        self.directory = directory
        self.active_statuses = active_statuses
        self.max_details, self.max_bytes = max_details, max_bytes
        self.cache = OrderedDict()

    def __getitem__(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if not re.fullmatch(r"[a-f0-9]{32}", str(key)):
            raise KeyError(key)
        try:
            value = json.loads((self.directory / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise KeyError(key) from None
        if not isinstance(value, dict) or value.get("id") != key:
            raise KeyError(key)
        self[key] = value
        return value

    def __setitem__(self, key, value):
        self.cache[key] = value
        self.cache.move_to_end(key)
        self.trim()

    def trim(self):
        completed = [(key, value) for key, value in self.cache.items()
                     if value.get("status") not in self.active_statuses]
        size = 0
        keep = set()
        for key, value in reversed(completed):
            length = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
            if len(keep) < self.max_details and size + length <= self.max_bytes:
                keep.add(key)
                size += length
        for key, _ in completed:
            if key not in keep and (self.directory / f"{key}.json").exists():
                self.cache.pop(key, None)

    def __delitem__(self, key):
        # Journals are business records; eviction never deletes one.
        if key not in self:
            raise KeyError(key)
        self.cache.pop(key, None)

    def __iter__(self):
        if self.directory.is_dir():
            for path in self.directory.glob("*.json"):
                if re.fullmatch(r"[a-f0-9]{32}", path.stem):
                    try:
                        self[path.stem]
                    except KeyError:
                        continue
                    yield path.stem
        for key in tuple(self.cache):
            if not (self.directory / f"{key}.json").exists():
                yield key

    def __len__(self):
        return sum(1 for _ in self)

    def recent(self, *, limit=100, before=None):
        limit = max(1, min(100, int(limit)))
        boundary = None
        if before:
            previous = self[before]
            boundary = (float(previous.get("created_at", 0)), before)

        def summaries():
            for key in self:
                try:
                    job = self[key]
                except KeyError:
                    continue
                order = (float(job.get("created_at", 0)), key)
                if boundary is None or order < boundary:
                    yield {name: value for name, value in job.items()
                           if name not in {"events", "key", "digest"}}

        return heapq.nlargest(limit, summaries(),
                              key=lambda row: (float(row.get("created_at", 0)), row["id"]))

