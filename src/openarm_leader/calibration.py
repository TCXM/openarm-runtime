"""Bounded, read-only capture of a stationary leader pose."""

from dataclasses import replace
import statistics
import time

from .mapping import delta


def collect_stable(readers, kind, *, clock=time.monotonic, sleep=time.sleep):
    """Collect 0.7 s of fresh feedback; return all arms or fail without mutation.

    Require >=10 distinct frames spanning >=0.5 s and <=12 encoder ticks
    peak-to-peak on the captured channels (about one degree).
    """
    if kind not in ("zero", "open", "closed"):
        raise ValueError("Unknown calibration kind")
    frames = {side: [] for side in readers}
    started = clock()
    while clock() - started < 0.7:
        now = clock()
        for side, reader in readers.items():
            sample, error, _ = reader.snapshot()
            if error or sample is None or not sample.fresh(now):
                raise ValueError(f"{side}：连接中断或读数过期，请重试")
            if any(sample.torque):
                raise ValueError(f"{side}：主臂已使能，无法采集手动标定")
            if any(e & ~1 for e in sample.errors):
                raise ValueError(f"{side}：舵机反馈异常")
            if not frames[side] or sample.received_at > frames[side][-1].received_at:
                frames[side].append(sample)
        sleep(0.02)
    result = {}
    indices = range(7) if kind == "zero" else (7,)
    for side, samples in frames.items():
        if (len(samples) < 10 or samples[-1].received_at - samples[0].received_at < 0.5
                or not samples[-1].fresh(clock())):
            raise ValueError(f"{side}：有效采样不足，请重试")
        positions = list(samples[-1].positions)
        for i in indices:
            origin = samples[0].positions[i]
            offsets = [delta(s.positions[i], origin) for s in samples]
            if max(offsets) - min(offsets) > 12:
                raise ValueError(f"{side}：采集期间发生移动，请保持静止后重试")
            positions[i] = (origin + round(statistics.median(offsets))) % 4096
        result[side] = replace(samples[-1], positions=tuple(positions))
    return result
