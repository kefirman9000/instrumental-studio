"""Write standard MIDI files that line up with the song.

The song's tempo is never perfectly steady, so instead of one BPM the tempo map has a tempo change on
every beat the app found: a note placed at "beat 37 and a half" lands exactly where it was played, and
music software that imports the file follows the song's own timing. Section names become markers.
"""
import struct

TPB = 480     # ticks per beat


def _vlq(n):
    n = int(max(0, n))
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _track(events):
    """events: [(tick, bytes)] -> MTrk chunk (sorted, delta times, end of track)."""
    events = sorted(events, key=lambda e: (e[0], e[2] if len(e) > 2 else 0))
    data, last = b"", 0
    for ev in events:
        tick, body = ev[0], ev[1]
        data += _vlq(tick - last) + body
        last = tick
    data += _vlq(0) + b"\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(data)) + data


class Clock:
    """Seconds <-> ticks along the song's beats."""

    def __init__(self, beat_times):
        bt = [float(t) for t in beat_times]
        if len(bt) < 2:
            bt = [0.0, 0.5]
        if bt[0] > 0.005:
            # extend the beats back to the very start, so tick 0 is the start of the audio (stems and MIDI
            # exported together line up when both are placed at the start of a project)
            import math
            step = bt[1] - bt[0]
            k = math.ceil(bt[0] / step)
            pre = [bt[0] - i * step for i in range(k, 0, -1)]
            pre[0] = 0.0
            bt = pre + bt
        self.bt = bt

    def tick(self, t):
        bt = self.bt
        if t <= bt[0]:
            return max(0, int(round((t - bt[0]) / (bt[1] - bt[0]) * TPB)))
        lo, hi = 0, len(bt) - 1
        if t >= bt[-1]:
            step = bt[-1] - bt[-2]
            return int(round((len(bt) - 1 + (t - bt[-1]) / step) * TPB))
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if bt[mid] <= t:
                lo = mid
            else:
                hi = mid
        return int(round((lo + (t - bt[lo]) / (bt[lo + 1] - bt[lo])) * TPB))

    def tempo_events(self):
        out, prev = [], None
        for j in range(len(self.bt) - 1):
            us = int(round((self.bt[j + 1] - self.bt[j]) * 1e6))
            us = min(max(us, 1), 0xFFFFFF)
            if prev is None or abs(us - prev) > prev * 0.002:      # skip changes nobody could hear
                out.append((j * TPB, b"\xff\x51\x03" + us.to_bytes(3, "big"), 0))
                prev = us
        return out


def _meta_text(kind, text):
    b = text.encode("utf-8")[:120]
    return bytes([0xFF, kind]) + _vlq(len(b)) + b


def write(path, beat_times, beats_per_bar=4, markers=(), tracks=(), name="Instrumental Studio"):
    """markers: [(seconds, text)]; tracks: [(name, channel, [(seconds, note, velocity, duration_s)])]."""
    clk = Clock(beat_times)
    first_offset = clk.bt[0]
    head = [(0, _meta_text(0x03, name), 0),
            (0, b"\xff\x58\x04" + bytes([int(beats_per_bar), 2, 24, 8]), 0)]        # time signature n/4
    head += clk.tempo_events()
    head += [(clk.tick(t), _meta_text(0x06, txt), 1) for t, txt in markers]
    chunks = [_track(head)]
    for tname, ch, notes in tracks:
        evs = [(0, _meta_text(0x03, tname), 0)]
        for t, note, vel, dur in notes:
            on = clk.tick(t)
            off = max(on + 1, clk.tick(t + dur))
            evs.append((on, bytes([0x90 | ch, note & 0x7F, max(1, min(127, int(vel)))]), 2))
            evs.append((off, bytes([0x80 | ch, note & 0x7F, 0]), 1))
        chunks.append(_track(evs))
    hdr = b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), TPB)
    with open(path, "wb") as f:
        f.write(hdr + b"".join(chunks))
    return first_offset
