"""Detached snapshot rows: capture changes under the owner lock, format outside."""
import json
from dataclasses import dataclass, field


class RowTable:
    """Copy changed chunks; a captured tuple never aliases a mutable chunk."""

    def __init__(self):
        self.positions = {}
        self.chunks = []
        self.changed = set()
        self.size = 0

    def __setitem__(self, key, value):
        position = self.positions.get(key)
        if position is None:
            position = self.size
            self.size += 1
            self.positions[key] = position
        block, offset = divmod(position, 512)
        if block == len(self.chunks):
            self.chunks.append([])
            self.changed.add(block)
        elif block not in self.changed:
            self.chunks[block] = list(self.chunks[block])
            self.changed.add(block)
        chunk = self.chunks[block]
        if offset == len(chunk):
            chunk.append(value)
        else:
            chunk[offset] = value

    def pop(self, key, default=None):
        if key not in self.positions:
            return default
        self[key] = None
        del self.positions[key]

    def capture(self):
        for block in self.changed:
            self.chunks[block] = tuple(self.chunks[block])
        self.changed.clear()
        return tuple(self.chunks)


class EdgeFrame:
    def __init__(self, key, row):
        self.key, self._row, self._encoded = key, row, None

    def row(self):
        return self._row

    def encoded(self):
        if self._encoded is None:
            self._encoded = json.dumps(self.row(), sort_keys=True, separators=(',', ':'))
        return self._encoded


@dataclass
class NodeFrame:
    key: str
    features: frozenset
    parents: tuple
    counts: tuple
    observations: frozenset
    _row: dict | None = field(default=None, repr=False)
    _encoded: str | None = field(default=None, repr=False)

    encoded = EdgeFrame.encoded

    def row(self):
        if self._row is None:
            self._row = {'id': self.key, 'features': sorted(self.features),
                         'parents': list(self.parents), 'counts': list(self.counts),
                         'observations': sorted(self.observations)}
        return self._row


class SnapshotPayload(dict):
    """Ordinary snapshot dictionary, with reusable immutable JSON rows.

    The owner writer streams bounded chunks, so a multi-GB C JSON encoder does
    not hold the GIL for seconds. Public JSON keys and bytes remain unchanged.
    """

    def __init__(self, obj, nodes, edges):
        super().__init__(obj)
        self.frames = {'nodes': nodes, 'edges': edges}

    def json_chunks(self):
        encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'))
        yield '{'
        for i, key in enumerate(sorted(self)):
            yield (',' if i else '') + json.dumps(key) + ':'
            frames = self.frames.get(key)
            if frames is None:
                yield from encoder.iterencode(self[key])
                continue
            yield '['
            buffer = []
            for j, frame in enumerate(frames):
                buffer.append((',' if j else '') + frame.encoded())
                if len(buffer) == 128:
                    yield ''.join(buffer)
                    buffer.clear()
            yield ''.join(buffer)
            yield ']'
        yield '}\n'
