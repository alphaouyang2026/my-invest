"""Run one experiment command under an external cgroup memory budget."""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from app.experiments.artifact_cache import write_json

GIB = 1024 ** 3


def over_limit(memory: int, available: int, *, stop_gib: float, reserve_gib: float) -> bool:
    return memory > stop_gib * GIB or available < reserve_gib * GIB


def read_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("MemAvailable is unavailable")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warning-gib", type=float, default=5.5)
    parser.add_argument("--stop-gib", type=float, default=6.0)
    parser.add_argument("--reserve-gib", type=float, default=1.55)
    parser.add_argument("--consecutive", type=int, default=3)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--grace-seconds", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.consecutive < 1 or args.interval <= 0:
        parser.error("a command, positive consecutive count and interval are required")
    if not 0 < args.warning_gib < args.stop_gib:
        parser.error("require 0 < warning-gib < stop-gib")

    cgroup = Path("/sys/fs/cgroup")
    events_before = (cgroup / "memory.events").read_text()
    process = subprocess.Popen(command)
    samples, streak, stopped = [], 0, False
    started = time.monotonic()
    while process.poll() is None:
        memory, available = int((cgroup / "memory.current").read_text()), read_available()
        limited = over_limit(memory, available, stop_gib=args.stop_gib, reserve_gib=args.reserve_gib)
        streak = streak + 1 if limited else 0
        samples.append(dict(elapsed_seconds=round(time.monotonic() - started, 3),
                            memory_bytes=memory, available_bytes=available,
                            warning=memory > args.warning_gib * GIB, over_limit=limited))
        if streak >= args.consecutive:
            stopped = True
            process.terminate()
            try:
                process.wait(timeout=args.grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            break
        time.sleep(args.interval)
    result = dict(command=command, exit_code=process.returncode, resource_limit=stopped,
                  thresholds=dict(warning_gib=args.warning_gib, stop_gib=args.stop_gib,
                                  reserve_gib=args.reserve_gib, consecutive=args.consecutive),
                  events_before=events_before, events_after=(cgroup / "memory.events").read_text(),
                  samples=samples)
    write_json(args.output, result)
    return 75 if stopped else int(process.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
