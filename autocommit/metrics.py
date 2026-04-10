#!/usr/bin/env python3
"""
Parses the structured metrics log produced by etc.py and prints a run summary.

Usage:
    python3 metrics.py patches/metrics.log
    python3 metrics.py patches/metrics.log --csv          # also write metrics.csv
    python3 metrics.py run1/metrics.log run2/metrics.log  # compare multiple runs
"""

import sys
import csv
import os
from dataclasses import dataclass, field
from typing import Optional


# ------------------------------------------------------------------ #
#  Data model                                                        #
# ------------------------------------------------------------------ #

@dataclass
class Iteration:
    number: int
    pending_at_start: int
    group_size: Optional[int] = None
    oracle_calls: int = 0
    duration_ms: int = 0
    status: str = "unknown"   # "commit" | "failed"
    hunks: str = ""


@dataclass
class RunSummary:
    source: str
    build_cmd: str = ""
    total_hunks: int = 0
    total_commits: int = 0
    total_skipped: int = 0
    total_oracle_calls: int = 0
    total_duration_ms: int = 0
    iterations: list = field(default_factory=list)
    interrupted: bool = False


# ------------------------------------------------------------------ #
#  Parser                                                            #
# ------------------------------------------------------------------ #

def parse_data(data_str: str) -> dict:
    """Parse 'key=val,key2=val2' into a dict."""
    result = {}
    for part in data_str.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result


def parse_log(path: str) -> RunSummary:
    summary = RunSummary(source=path)
    current_iter: Optional[Iteration] = None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            _ts, event, data_str = parts
            data = parse_data(data_str)

            if event == "RUN_START":
                summary.total_hunks = int(data.get("total_hunks", 0))
                summary.build_cmd = data.get("build_cmd", "")

            elif event == "ITER_START":
                current_iter = Iteration(
                    number=int(data.get("iteration", 0)),
                    pending_at_start=int(data.get("pending", 0)),
                )

            elif event == "ITER_COMMIT":
                if current_iter:
                    current_iter.group_size = int(data.get("group_size", 0))
                    current_iter.oracle_calls = int(data.get("oracle_calls", 0))
                    current_iter.duration_ms = int(data.get("duration_ms", 0))
                    current_iter.status = "commit"
                    current_iter.hunks = data.get("hunks", "")
                    summary.iterations.append(current_iter)
                    summary.total_oracle_calls += current_iter.oracle_calls
                    summary.total_duration_ms += current_iter.duration_ms
                    current_iter = None

            elif event == "ITER_FAILED":
                if current_iter:
                    current_iter.oracle_calls = int(data.get("oracle_calls", 0))
                    current_iter.duration_ms = int(data.get("duration_ms", 0))
                    current_iter.status = "failed"
                    current_iter.group_size = 0
                    summary.iterations.append(current_iter)
                    summary.total_oracle_calls += current_iter.oracle_calls
                    summary.total_duration_ms += current_iter.duration_ms
                    current_iter = None

            elif event == "RUN_END":
                summary.total_commits = int(data.get("commits", 0))
                summary.total_skipped = int(data.get("skipped", 0))

            elif event == "INTERRUPTED":
                summary.interrupted = True

    return summary


# ------------------------------------------------------------------ #
#  Display                                                           #
# ------------------------------------------------------------------ #

def fmt_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms/1000:.1f}s"


def print_summary(s: RunSummary) -> None:
    width = 60
    print("=" * width)
    print(f"  ETC Metrics Report")
    print(f"  Source : {s.source}")
    print(f"  Build  : {s.build_cmd}")
    if s.interrupted:
        print("  ⚠  Run was interrupted")
    print("=" * width)

    print(f"\n{'OVERVIEW':}")
    print(f"  Total hunks          : {s.total_hunks}")
    print(f"  Commits produced     : {s.total_commits}")
    print(f"  Skipped hunks        : {s.total_skipped}")
    print(f"  Iterations           : {len(s.iterations)}")
    print(f"  Total oracle calls   : {s.total_oracle_calls}")
    print(f"  Total wall time      : {fmt_ms(s.total_duration_ms)}")
    if s.total_commits > 0:
        avg = s.total_oracle_calls / s.total_commits
        print(f"  Avg oracle calls/commit : {avg:.1f}")

    print(f"\n{'PER-ITERATION BREAKDOWN':}")
    header = f"  {'Iter':>4}  {'Pending':>7}  {'Group':>5}  {'Oracles':>7}  {'Time':>7}  {'Status'}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for it in s.iterations:
        group_str = str(it.group_size) if it.group_size is not None else "-"
        status_icon = "✓" if it.status == "commit" else "✗"
        print(
            f"  {it.number:>4}  {it.pending_at_start:>7}  {group_str:>5}  "
            f"{it.oracle_calls:>7}  {fmt_ms(it.duration_ms):>7}  {status_icon} {it.status}"
        )

    print()


def write_csv(s: RunSummary, out_path: str) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "iteration", "pending_at_start", "group_size",
            "oracle_calls", "duration_ms", "status"
        ])
        for it in s.iterations:
            writer.writerow([
                it.number, it.pending_at_start, it.group_size,
                it.oracle_calls, it.duration_ms, it.status
            ])
    print(f"  CSV written to: {out_path}")


def compare_runs(summaries: list) -> None:
    """Print a side-by-side comparison table for multiple runs."""
    print("\n" + "=" * 70)
    print("  COMPARISON")
    print("=" * 70)
    labels = [os.path.dirname(s.source) or s.source for s in summaries]
    col = 14

    def row(label, values):
        print(f"  {label:<28}", end="")
        for v in values:
            print(f"{str(v):>{col}}", end="")
        print()

    header_vals = [f"[{l}]" for l in labels]
    row("", header_vals)
    print("  " + "-" * (28 + col * len(summaries)))
    row("Total hunks",     [s.total_hunks for s in summaries])
    row("Commits",         [s.total_commits for s in summaries])
    row("Skipped hunks",   [s.total_skipped for s in summaries])
    row("Iterations",      [len(s.iterations) for s in summaries])
    row("Total oracles",   [s.total_oracle_calls for s in summaries])
    row("Total time",      [fmt_ms(s.total_duration_ms) for s in summaries])
    row("Avg oracles/commit", [
        f"{s.total_oracle_calls/s.total_commits:.1f}" if s.total_commits else "-"
        for s in summaries
    ])
    print()


# ------------------------------------------------------------------ #
#  Entry point                                                       #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    write_csv_flag = "--csv" in flags

    if not args:
        print("Usage: python3 metrics.py <metrics.log> [<metrics2.log> ...] [--csv]")
        sys.exit(1)

    summaries = []
    for path in args:
        if not os.path.exists(path):
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        s = parse_log(path)
        summaries.append(s)
        print_summary(s)

        if write_csv_flag:
            csv_path = path.replace(".log", ".csv")
            write_csv(s, csv_path)

    if len(summaries) > 1:
        compare_runs(summaries)
