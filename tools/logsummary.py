"""Condense a Wine run log into a bounded, grep-friendly summary.

The summary is what agents should read first; raw logs are only for targeted greps.
"""
from __future__ import annotations

import gzip
import re
from collections import Counter
from pathlib import Path

WINE_LINE = re.compile(
    r"^(?:(?P<ts>\d+\.\d+):)?(?:[0-9a-f]{4,}:)?(?P<tid>[0-9a-f]{4,}):"
    r"(?P<cls>trace|fixme|err|warn):(?P<ch>[\w-]+):(?P<fn>\S*)\s?(?P<msg>.*)$"
)
QT_LINE = re.compile(r"^\[qt ")
LOADDLL = re.compile(r'Loaded L"(?P<path>[^"]+)".*?:\s*(?P<kind>native|builtin)')
CRASH = re.compile(r"Unhandled (exception|page fault)|Backtrace:|wine: (Call|Unimplemented)|EXCEPTION_|segfault", re.I)

_NORMALISERS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x#"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "#"),
    (re.compile(r"\b\d+\b"), "N"),
]


def normalise(text: str, limit: int = 180) -> str:
    for pattern, repl in _NORMALISERS:
        text = pattern.sub(repl, text)
    return text[:limit]


def open_log(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="replace")
    return path.open("r", errors="replace")


def _top(counter: Counter, n: int) -> list[str]:
    return [f"{count:>8}  {key}" for key, count in counter.most_common(n)]


def summarise_wine_log(path: Path) -> tuple[list[str], dict]:
    by_class: Counter = Counter()
    by_channel: Counter = Counter()
    by_func: Counter = Counter()
    errs: Counter = Counter()
    fixmes: Counter = Counter()
    comm: Counter = Counter()
    debugstr: list[str] = []
    other: Counter = Counter()
    qt: Counter = Counter()
    crashes: list[str] = []
    dlls: dict[str, str] = {}
    total = 0
    first_ts = last_ts = None

    with open_log(path) as handle:
        for raw in handle:
            total += 1
            line = raw.rstrip("\n")
            m = WINE_LINE.match(line)
            if CRASH.search(line) and len(crashes) < 20:
                crashes.append(line[:240])
            if not m:
                if line.strip():
                    (qt if QT_LINE.match(line) else other)[normalise(line)] += 1
                continue
            if m["ts"]:
                ts = float(m["ts"])
                first_ts = ts if first_ts is None else first_ts
                last_ts = ts
            cls, ch, fn, msg = m["cls"], m["ch"], m["fn"], m["msg"]
            by_class[cls] += 1
            by_channel[f"{cls}:{ch}"] += 1
            by_func[f"{ch}:{fn}"] += 1
            if cls in ("err", "warn"):
                errs[normalise(f"{cls}:{ch}:{fn} {msg}")] += 1
            elif cls == "fixme":
                fixmes[normalise(f"{ch}:{fn} {msg}")] += 1
            if ch == "comm":
                comm[normalise(f"{fn} {msg}", 120)] += 1
            elif ch == "debugstr":
                debugstr.append(msg[:200])
            elif ch == "loaddll":
                dm = LOADDLL.search(msg)
                if dm:
                    dlls[dm["path"].split("\\")[-1].lower()] = dm["kind"]

    native = sorted(name for name, kind in dlls.items() if kind == "native")
    stats = {
        "lines": total,
        "err": by_class["err"],
        "warn": by_class["warn"],
        "fixme": by_class["fixme"],
        "trace": by_class["trace"],
        "comm_lines": sum(comm.values()),
        "crash_markers": len(crashes),
        "qt_lines": sum(qt.values()),
        "wine_span_s": round(last_ts - first_ts, 1) if first_ts is not None else None,
    }

    out = [f"## wine.log  ({total} lines, span {stats['wine_span_s']}s)",
           "counts: " + ", ".join(f"{k}={by_class[k]}" for k in ("err", "warn", "fixme", "trace"))]
    if crashes:
        out += ["", "### CRASH / EXCEPTION markers"] + crashes
    out += ["", "### top channels"] + _top(by_channel, 12)
    out += ["", "### top err/warn (normalised, count)"] + (_top(errs, 25) or ["(none)"])
    out += ["", "### top fixme"] + (_top(fixmes, 12) or ["(none)"])
    if comm:
        out += ["", "### comm (serial) calls"] + _top(comm, 25)
    if debugstr:
        uniq = list(dict.fromkeys(debugstr))
        out += ["", f"### OutputDebugString ({len(debugstr)} lines, last 25 unique)"] + uniq[-25:]
    if qt:
        out += ["", f"### Qt messages ({sum(qt.values())} lines)"] + _top(qt, 30)
    if other:
        out += ["", "### other stderr (non-Wine-format)"] + _top(other, 15)
    out += ["", f"### native DLLs loaded ({len(native)}/{len(dlls)})", ", ".join(native) or "(none)"]
    return out, stats


def is_text(path: Path) -> bool:
    sample = path.read_bytes()[:4096]
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in sample)
    return not sample or printable / len(sample) > 0.95


def summarise_text(path: Path, title: str, limit: int = 20) -> list[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open_log(path) as handle:
        lines = [l.rstrip() for l in handle if l.strip()]
    return ["", f"## {title} ({len(lines)} lines, last {limit})"] + [l[:200] for l in lines[-limit:]]
