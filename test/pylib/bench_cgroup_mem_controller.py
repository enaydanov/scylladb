#!/usr/bin/env python3
#
# Copyright (C) 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#
"""Micro-benchmark: cgroup memory controller overhead on page allocation.

Measures the performance impact of per-worker cgroup memory controller
isolation on page allocation throughput. Simulates the CI topology where
each xdist worker runs multiple Scylla nodes in its own memory-controlled
cgroup.

The benchmark answers: "Does the per-worker cgroup topology used by
--gather-metrics hurt or help memory allocation performance compared to
all processes sharing a single cgroup?"

Design:
  - Fixed number of cgroups = CPU count (simulates xdist workers)
  - Varying group_size = processes per cgroup (simulates Scylla nodes)
  - Total processes = num_cgroups x group_size

  Baseline:  all processes in the container cgroup (shared memcg/lruvec)
  +memory:   processes distributed into num_cgroups leaf cgroups

The workload per process:
  - mmap(64 MiB, MAP_ANONYMOUS | MAP_PRIVATE)
  - Touch every page sequentially (triggers page fault -> mem_cgroup_charge)
  - munmap (triggers mem_cgroup_uncharge)
  - Repeat N iterations

All workers use a stdin-pipe barrier for synchronization: they block on
read(stdin, 1) until the orchestrator releases them simultaneously. This
eliminates staggering artifacts from sequential cgroup migration.

Usage (inside toolchain container):
    python3 test/pylib/bench_cgroup_mem_controller.py
    python3 test/pylib/bench_cgroup_mem_controller.py --num-cgroups 16
    python3 test/pylib/bench_cgroup_mem_controller.py --group-sizes 1,2,3,4,6,8
    python3 test/pylib/bench_cgroup_mem_controller.py --buf-size-mb 128 --iterations 50
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Ensure the project root is on sys.path so "test" package is importable
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from test.pylib.resource_gather import (
    CGROUP_TESTS,
    setup_cgroup,
)


# --- Embedded C source ---

MEM_ALLOC_BENCH_C = r"""
/*
 * mem_alloc_bench.c - Page allocation/deallocation throughput benchmark.
 *
 * Exercises the kernel page fault and unmap paths that are affected by
 * the cgroup v2 memory controller (mem_cgroup_charge/uncharge).
 *
 * Usage: ./mem_alloc_bench <buffer_size_bytes> <iterations> [--barrier]
 * Output: <pages_per_sec> <elapsed_sec> <total_pages>
 *
 * When --barrier is specified, the process blocks on stdin (reads 1 byte)
 * before starting the timed workload. This allows the orchestrator to
 * synchronize all workers so they begin simultaneously, eliminating
 * staggering artifacts from cgroup migration.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    if (argc < 3 || argc > 4) {
        fprintf(stderr, "Usage: %s <buffer_size_bytes> <iterations> [--barrier]\n",
                argv[0]);
        return 1;
    }

    long buf_size = atol(argv[1]);
    int iterations = atoi(argv[2]);
    int use_barrier = (argc == 4 && strcmp(argv[3], "--barrier") == 0);
    long page_size = sysconf(_SC_PAGESIZE);
    long num_pages = buf_size / page_size;

    /* If barrier requested, block until orchestrator releases us */
    if (use_barrier) {
        char c;
        ssize_t n = read(STDIN_FILENO, &c, 1);
        if (n != 1) {
            fprintf(stderr, "barrier: read from stdin failed\n");
            return 1;
        }
    }

    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);

    long total_pages = 0;
    for (int i = 0; i < iterations; i++) {
        /* Allocate anonymous pages (not yet backed by physical memory) */
        void *buf = mmap(NULL, buf_size, PROT_READ | PROT_WRITE,
                         MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
        if (buf == MAP_FAILED) {
            perror("mmap");
            return 1;
        }

        /* Touch every page: triggers page fault -> mem_cgroup_charge() */
        volatile char *p = (volatile char *)buf;
        for (long offset = 0; offset < buf_size; offset += page_size) {
            p[offset] = 1;
        }

        /* Free all pages: triggers mem_cgroup_uncharge() */
        munmap(buf, buf_size);
        total_pages += num_pages;
    }

    clock_gettime(CLOCK_MONOTONIC, &end);
    double elapsed = (end.tv_sec - start.tv_sec) +
                     (end.tv_nsec - start.tv_nsec) / 1e9;
    double pages_per_sec = total_pages / elapsed;

    printf("%.0f %.6f %ld\n", pages_per_sec, elapsed, total_pages);
    return 0;
}
"""


# --- Compilation ---

def compile_bench(build_dir: Path) -> Path:
    """Compile the C benchmark binary. Returns path to executable."""
    src_path = build_dir / "mem_alloc_bench.c"
    bin_path = build_dir / "mem_alloc_bench"

    src_path.write_text(MEM_ALLOC_BENCH_C)

    result = subprocess.run(
        ["gcc", "-O2", "-Wall", "-o", str(bin_path), str(src_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Failed to compile benchmark:\n{result.stderr}",
              file=sys.stderr)
        sys.exit(1)

    return bin_path


# --- Cgroup management ---

def setup_bench_cgroup(num_cgroups: int) -> Optional[Path]:
    """Create cgroup hierarchy with +memory for benchmark.

    Creates num_cgroups leaf cgroups under CGROUP_TESTS/bench_mem_ctrl.
    Returns the parent cgroup path, or None if memory controller unavailable.
    """
    # Check memory controller is available
    try:
        with open(CGROUP_TESTS / "cgroup.controllers", "r") as f:
            available = f.readline().split()
    except (FileNotFoundError, PermissionError):
        return None

    if "memory" not in available:
        print("WARNING: memory controller not available at "
              f"{CGROUP_TESTS}/cgroup.controllers")
        return None

    # Enable +memory on CGROUP_TESTS subtree_control
    try:
        with open(CGROUP_TESTS / "cgroup.subtree_control", "w") as f:
            f.write("+memory")
    except OSError as e:
        print(f"WARNING: cannot enable +memory: {e}")
        return None

    # Create parent for this benchmark
    parent = CGROUP_TESTS / "bench_mem_ctrl"
    parent.mkdir(exist_ok=True)

    # Enable +memory in parent's subtree_control
    try:
        with open(parent / "cgroup.subtree_control", "w") as f:
            f.write("+memory")
    except OSError as e:
        print(f"WARNING: cannot propagate +memory to bench parent: {e}")
        return None

    # Pre-create leaf cgroup directories
    for i in range(num_cgroups):
        worker_dir = parent / f"worker_{i}"
        worker_dir.mkdir(exist_ok=True)
        # Enable memory in worker subtree for the leaf
        try:
            with open(worker_dir / "cgroup.subtree_control", "w") as f:
                f.write("+memory")
        except OSError:
            pass
        leaf = worker_dir / "default"
        leaf.mkdir(exist_ok=True)

    return parent


def teardown_bench_cgroup(num_cgroups: int) -> None:
    """Remove benchmark cgroup hierarchy."""
    parent = CGROUP_TESTS / "bench_mem_ctrl"
    if not parent.exists():
        return

    for i in range(num_cgroups):
        worker_dir = parent / f"worker_{i}"
        leaf = worker_dir / "default"
        for d in (leaf, worker_dir):
            if d.exists():
                try:
                    d.rmdir()
                except OSError:
                    pass
    try:
        parent.rmdir()
    except OSError:
        pass


def move_to_cgroup(pid: int, cgroup_parent: Path, cgroup_id: int) -> bool:
    """Move a process into the specified leaf cgroup."""
    leaf = cgroup_parent / f"worker_{cgroup_id}" / "default"
    try:
        with open(leaf / "cgroup.procs", "w") as f:
            f.write(str(pid))
        return True
    except OSError as e:
        print(f"  WARNING: cannot move PID {pid} to {leaf}: {e}")
        return False


# --- Benchmark execution ---

def run_workers(total_procs: int, bin_path: Path, buf_size: int,
                iterations: int, cgroup_parent: Optional[Path],
                num_cgroups: int) -> list[float]:
    """Run total_procs workers in parallel, return list of pages/s per worker.

    If cgroup_parent is None, all workers stay in the container cgroup
    (shared memcg -- simulates CI without --gather-metrics).

    If cgroup_parent is set, workers are distributed evenly across
    num_cgroups leaf cgroups (simulates CI with --gather-metrics where
    each xdist worker has its own cgroup with multiple Scylla nodes).

    All workers use a stdin-based barrier: they block on read(stdin, 1)
    until the orchestrator releases them simultaneously.
    """
    procs: list[subprocess.Popen] = []
    args = [str(bin_path), str(buf_size), str(iterations), "--barrier"]

    # Spawn all workers (they block on stdin read immediately)
    for i in range(total_procs):
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        procs.append(proc)

    # Move to cgroups if requested (workers are idle, waiting on barrier)
    if cgroup_parent is not None:
        for i, proc in enumerate(procs):
            # Distribute evenly: process i goes to cgroup (i * num_cgroups // total_procs)
            cgroup_id = i * num_cgroups // total_procs
            if not move_to_cgroup(proc.pid, cgroup_parent, cgroup_id):
                # Kill all and abort this run
                for p in procs:
                    p.kill()
                    p.wait()
                return []

    # Release all workers simultaneously (tight loop: ~N microseconds total)
    for proc in procs:
        proc.stdin.write(b"\x01")
        proc.stdin.flush()

    # Wait for all workers to finish
    results: list[float] = []
    for i, proc in enumerate(procs):
        stdout, stderr = proc.communicate(timeout=300)
        if proc.returncode != 0:
            print(f"  Worker {i} failed (rc={proc.returncode}): "
                  f"{stderr.decode(errors='replace').strip()}")
            results.append(0.0)
            continue
        try:
            parts = stdout.decode().strip().split()
            pages_per_sec = float(parts[0])
            results.append(pages_per_sec)
        except (ValueError, IndexError) as e:
            print(f"  Worker {i} bad output: {stdout.decode().strip()}: {e}")
            results.append(0.0)

    return results


def run_scaling_test(bin_path: Path, buf_size: int, iterations: int,
                     num_cgroups: int, group_sizes: list[int], repeats: int,
                     cgroup_parent: Optional[Path]) -> list[tuple[int, int, float]]:
    """Run scaling test over group_sizes with fixed num_cgroups.

    For each group_size, spawns num_cgroups * group_size processes.
    In baseline (cgroup_parent=None), all stay in container cgroup.
    In +memory (cgroup_parent set), distributed into num_cgroups cgroups.

    Returns list of (group_size, total_procs, median_aggregate_pages_per_sec).
    """
    results: list[tuple[int, int, float]] = []

    for gs in group_sizes:
        total_procs = num_cgroups * gs
        print(f"    group_size={gs:>2}  total_procs={total_procs:>4} ",
              end="", flush=True)

        samples: list[float] = []
        for r in range(repeats):
            worker_results = run_workers(
                total_procs, bin_path, buf_size, iterations,
                cgroup_parent=cgroup_parent, num_cgroups=num_cgroups)
            if worker_results:
                total = sum(worker_results)
                samples.append(total)

        median_val = statistics.median(samples) if samples else 0.0
        if median_val > 0:
            print(f"{median_val / 1e6:>7.2f} Mpages/s")
        else:
            print("FAILED")

        results.append((gs, total_procs, median_val))

    return results


# --- Main ---

def run_benchmark(args: argparse.Namespace) -> None:
    """Run the full memory controller overhead benchmark."""
    num_cgroups = args.num_cgroups or os.cpu_count() or 1
    group_sizes = [int(x) for x in args.group_sizes.split(",")]
    buf_size = args.buf_size_mb * 1024 * 1024
    iterations = args.iterations
    repeats = args.repeats
    page_size = os.sysconf("SC_PAGESIZE")
    pages_per_iter = buf_size // page_size
    arch = platform.machine()

    print("Memory controller overhead: page allocation throughput")
    print("=" * 70)
    print(f"Architecture: {arch}")
    print(f"CPUs:         {os.cpu_count()}")
    print(f"Page size:    {page_size} bytes")
    print(f"Buffer size:  {args.buf_size_mb} MiB ({pages_per_iter} pages per iteration)")
    print(f"Iterations:   {iterations} per worker")
    print(f"Repeats:      {repeats} per data point (median taken)")
    print(f"Cgroups:      {num_cgroups} (simulates xdist workers)")
    print(f"Group sizes:  {group_sizes} (processes per cgroup)")
    max_total = num_cgroups * max(group_sizes)
    print(f"Max procs:    {max_total} ({num_cgroups} cgroups x {max(group_sizes)} procs)")
    print()

    # Compile C benchmark
    build_dir = Path(tempfile.mkdtemp(prefix="bench_mem_ctrl_"))
    try:
        print("Compiling benchmark binary...", flush=True)
        bin_path = compile_bench(build_dir)
        print(f"  {bin_path}")
        print()

        # Quick sanity check
        result = subprocess.run(
            [str(bin_path), str(buf_size), "1"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"ERROR: Sanity check failed: {result.stderr}")
            sys.exit(1)
        sanity_pps = float(result.stdout.strip().split()[0])
        print(f"Sanity check: {sanity_pps / 1e6:.2f} Mpages/s (single run, 1 iter)")
        print()

        # PHASE 1: Baseline measurements (BEFORE cgroup setup).
        # Workers stay in the container cgroup (shared memcg).
        print("Phase 1: Baseline (all procs in container cgroup)")
        print("-" * 70)
        baseline_results = run_scaling_test(
            bin_path, buf_size, iterations, num_cgroups, group_sizes,
            repeats, cgroup_parent=None)
        print("-" * 70)
        print()

        # PHASE 2: Set up cgroup hierarchy and run +memory measurements.
        # Workers are distributed into per-worker leaf cgroups with +memory.
        print(f"Phase 2: +memory ({num_cgroups} cgroups, distributed)")
        setup_cgroup(True)
        cgroup_parent = setup_bench_cgroup(num_cgroups)
        if cgroup_parent is None:
            print("  WARNING: Could not set up cgroup with +memory controller.")
            print("  Cannot compare -- only baseline data available.")
            print()
            combined = [(gs, tp, b, b) for gs, tp, b in baseline_results]
            print_results(combined, arch, num_cgroups)
        else:
            print("-" * 70)
            memory_results = run_scaling_test(
                bin_path, buf_size, iterations, num_cgroups, group_sizes,
                repeats, cgroup_parent=cgroup_parent)
            print("-" * 70)
            print()

            # Merge baseline and +memory results
            combined: list[tuple[int, int, float, float]] = []
            for (gs_b, tp_b, baseline_val), (gs_m, tp_m, memory_val) in zip(
                    baseline_results, memory_results):
                assert gs_b == gs_m, f"Group size mismatch: {gs_b} vs {gs_m}"
                combined.append((gs_b, tp_b, baseline_val, memory_val))

            print_results(combined, arch, num_cgroups)

            # Cleanup cgroup
            teardown_bench_cgroup(num_cgroups)

    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def print_results(results: list[tuple[int, int, float, float]],
                  arch: str, num_cgroups: int) -> None:
    """Print formatted results and recommendation."""
    print("Summary")
    print("=" * 78)
    print(f" {'Group':>5} | {'Total':>5} | {'Baseline (Mpages/s)':>20} | "
          f"{'  +memory (Mpages/s)':>20} | {'Overhead':>9}")
    print(f" {'size':>5} | {'procs':>5} | {'(shared cgroup)':>20} | "
          f"{'(per-worker cgroups)':>20} |")
    print("-" * 78)

    overheads: list[tuple[int, int, float]] = []

    for gs, total_procs, baseline, memory in results:
        if baseline > 0 and memory > 0:
            overhead_pct = ((memory - baseline) / baseline) * 100.0
            overheads.append((gs, total_procs, overhead_pct))
            print(f" {gs:>5} | {total_procs:>5} | {baseline / 1e6:>20.2f} | "
                  f"{memory / 1e6:>20.2f} | {overhead_pct:>+8.1f}%")
        elif baseline > 0:
            print(f" {gs:>5} | {total_procs:>5} | {baseline / 1e6:>20.2f} | "
                  f"{'N/A':>20} | {'N/A':>9}")
        else:
            print(f" {gs:>5} | {total_procs:>5} | {'FAILED':>20} | "
                  f"{'FAILED':>20} | {'N/A':>9}")

    print("-" * 78)
    print()

    if not overheads:
        print("No overhead data collected. Cannot make recommendation.")
        return

    # Analyze results
    print(f"Topology: {num_cgroups} cgroups on {arch}")
    print()

    # Check if overhead decreases (benefit shrinks) with group_size
    negative_overheads = [(gs, tp, pct) for gs, tp, pct in overheads if pct < -5.0]
    positive_overheads = [(gs, tp, pct) for gs, tp, pct in overheads if pct > 5.0]

    if negative_overheads:
        # Per-worker cgroups cause slowdown at some group sizes
        worst_gs, worst_tp, worst_pct = min(negative_overheads, key=lambda x: x[2])
        print(f"SLOWDOWN detected: {worst_pct:+.1f}% at group_size={worst_gs} "
              f"({worst_tp} total procs)")
        reduction_pct = min(abs(worst_pct) * 0.75, 50.0)
        suggested = max(1, int(num_cgroups * (1 - reduction_pct / 100.0)))
        print(f"RECOMMENDATION: Reduce xdist workers by ~{reduction_pct:.0f}% on {arch}")
        print(f"  Current workers: {num_cgroups}")
        print(f"  Suggested workers: {suggested}")
    elif positive_overheads:
        # Per-worker cgroups are faster (lruvec isolation benefit)
        best_gs, best_tp, best_pct = max(positive_overheads, key=lambda x: x[2])
        print(f"Per-worker cgroups are FASTER: up to {best_pct:+.1f}% at "
              f"group_size={best_gs} ({best_tp} total procs)")
        print()
        print("CONCLUSION: The per-worker cgroup topology used by --gather-metrics")
        print("IMPROVES memory allocation scalability (per-memcg lruvec isolation")
        print("eliminates shared lock contention). Memory controller is NOT the")
        print("cause of flaky test failures.")
    else:
        print("CONCLUSION: Memory controller overhead is negligible (<5%).")
        print("Per-worker cgroups have no measurable impact on allocation throughput.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure cgroup memory controller overhead on "
                    "page allocation throughput")
    parser.add_argument(
        "--buf-size-mb", type=int, default=64,
        help="Buffer size per mmap/munmap cycle in MiB (default: 64)")
    parser.add_argument(
        "--iterations", type=int, default=100,
        help="Iterations per worker (default: 100)")
    parser.add_argument(
        "--num-cgroups", type=int, default=None,
        help="Number of cgroups / simulated xdist workers (default: CPU count)")
    parser.add_argument(
        "--group-sizes", type=str, default="1,2,3,4,6",
        help="Comma-separated list of processes per cgroup to test "
             "(default: 1,2,3,4,6)")
    parser.add_argument(
        "--repeats", type=int, default=3,
        help="Repeats per data point for median (default: 3)")
    args = parser.parse_args()

    run_benchmark(args)


if __name__ == "__main__":
    main()
