#!/usr/bin/env python3
#
# Copyright (C) 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#
"""Benchmark: cgroup controller overhead under parallel Scylla workload.

Measures the impact of cgroup memory controller + monitoring on Scylla
page-cache throughput by running N parallel workers (each with a 3-node
cluster) under different cgroup configurations.

The parallelism level matches the real CI environment (computed via
ThreadsCalculator, same as test.py uses for pytest-xdist workers).

Configurations tested:
  baseline          - No cgroup controllers, no monitoring
  memory_monitored  - +memory controller + ResourceGatherOn monitoring (1s reads)
  all_monitored     - +memory +cpu +io +pids controllers + monitoring

Usage (inside toolchain container or with Scylla built):
    python3 test/pylib/bench_cgroup_overhead.py --scylla-path build/dev/scylla
    python3 test/pylib/bench_cgroup_overhead.py --workers 4 --iterations 3
"""

from __future__ import annotations

import argparse
import asyncio
import math
import multiprocessing
import os
import shutil
import signal
import socket
import statistics
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

# Ensure the project root is on sys.path so "test" package is importable
# when running this script directly (not via pytest).
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import yaml

# We need cassandra-driver
try:
    from cassandra.cluster import Cluster as CqlCluster
    from cassandra.cluster import ExecutionProfile, EXEC_PROFILE_DEFAULT
    from cassandra.cluster import NoHostAvailable
    from cassandra.policies import RoundRobinPolicy
    from cassandra.query import BatchStatement, BatchType, SimpleStatement
except ImportError:
    print(
        "ERROR: cassandra-driver not installed. "
        "Run inside toolchain container or pip install cassandra-driver",
        file=sys.stderr,
    )
    sys.exit(1)

# Import cgroup infrastructure from the test framework
from test import TOP_SRC_DIR
from test.pylib.resource_gather import (
    CGROUP_TESTS,
    ResourceGatherOn,
    gather_host_info,
    setup_cgroup,
)
from test.pylib.db.writer import SQLiteWriter, DEFAULT_DB_NAME, HOST_INFO_TABLE


# --- Constants ---

# Matches test/pylib/scylla_cluster.py SCYLLA_CMDLINE_OPTIONS but with --smp 1
# to allow more parallel nodes within the same memory budget.
SCYLLA_CMDLINE_OPTIONS = [
    "--smp", "1",
    "-m", "1G",
    "--collectd", "0",
    "--overprovisioned",
    "--max-networking-io-control-blocks", "1000",
    "--unsafe-bypass-fsync", "1",
    "--kernel-page-cache", "1",
    "--commitlog-use-o-dsync", "0",
    "--abort-on-lsa-bad-alloc", "1",
    "--abort-on-seastar-bad-alloc",
    "--abort-on-internal-error", "1",
    "--abort-on-ebadf", "1",
]

# Number of Scylla nodes per worker (forms a real cluster via gossip + raft)
NODES_PER_WORKER = 3

# Memory per node in bytes (must match -m flag above)
MEMORY_PER_NODE = 1 * 1024 * 1024 * 1024  # 1 GiB

# Scylla default ports (no conflicts since each node has a unique IP)
CQL_PORT = 9042
STORAGE_PORT = 7000
API_PORT = 10000


# --- Data structures ---

@dataclass
class IterationResult:
    """Results from a single benchmark iteration (one worker)."""
    worker_id: int
    insert_time: float  # seconds for INSERT phase
    scan_time: float    # seconds for SCAN phase
    total_time: float   # insert_time + scan_time
    insert_ops: float   # rows/s during INSERT
    scan_ops: float     # rows/s during SCAN
    batch_latencies_ms: list[float] = field(default_factory=list)

    @property
    def p50_ms(self) -> float:
        if not self.batch_latencies_ms:
            return 0.0
        sorted_lat = sorted(self.batch_latencies_ms)
        idx = int(len(sorted_lat) * 0.50)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def p99_ms(self) -> float:
        if not self.batch_latencies_ms:
            return 0.0
        sorted_lat = sorted(self.batch_latencies_ms)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]


@dataclass
class ParallelIterationResult:
    """Aggregated results from one parallel iteration (all workers)."""
    wall_clock: float  # wall-clock time for all workers to finish
    worker_results: list[IterationResult] = field(default_factory=list)
    failed_workers: int = 0

    @property
    def mean_worker_time(self) -> float:
        if not self.worker_results:
            return 0.0
        return statistics.mean(r.total_time for r in self.worker_results)

    @property
    def stddev_worker_time(self) -> float:
        if len(self.worker_results) < 2:
            return 0.0
        return statistics.stdev(r.total_time for r in self.worker_results)

    @property
    def mean_insert_ops(self) -> float:
        if not self.worker_results:
            return 0.0
        return statistics.mean(r.insert_ops for r in self.worker_results)

    @property
    def mean_scan_ops(self) -> float:
        if not self.worker_results:
            return 0.0
        return statistics.mean(r.scan_ops for r in self.worker_results)


@dataclass
class ConfigResult:
    """Aggregated results for a configuration across iterations."""
    name: str
    num_workers: int
    iterations: list[ParallelIterationResult] = field(default_factory=list)

    @property
    def mean_wall_clock(self) -> float:
        return statistics.mean(r.wall_clock for r in self.iterations)

    @property
    def stddev_wall_clock(self) -> float:
        if len(self.iterations) < 2:
            return 0.0
        return statistics.stdev(r.wall_clock for r in self.iterations)

    @property
    def mean_worker_time(self) -> float:
        return statistics.mean(r.mean_worker_time for r in self.iterations)

    @property
    def mean_insert_ops(self) -> float:
        return statistics.mean(r.mean_insert_ops for r in self.iterations)

    @property
    def mean_scan_ops(self) -> float:
        return statistics.mean(r.mean_scan_ops for r in self.iterations)


# --- Worker count calculation ---

def compute_num_workers(override: Optional[int]) -> tuple[int, str]:
    """Compute number of parallel workers.

    Returns (num_workers, reasoning_string) so the output can explain
    why a particular worker count was chosen.

    The CPU constraint accounts for the fact that each worker runs
    NODES_PER_WORKER Scylla instances simultaneously during startup.
    Unlike real CI (where tests stagger), the benchmark starts all
    workers at once, so we must avoid severe CPU oversubscription
    that causes raft/gossip timeouts.
    """
    if override is not None:
        return (max(1, override), "manual")

    nr_cpus = os.cpu_count() or 1
    sys_mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")

    # Memory constraints
    system_reserve = min(max(sys_mem / 16.0, 5e9), 8e9)
    available_mem = max(0, sys_mem - system_reserve)
    max_by_node_mem = max(1, int(available_mem / (NODES_PER_WORKER * MEMORY_PER_NODE)))

    # CPU constraint: each worker has NODES_PER_WORKER Scylla processes
    # that all need CPU during startup (raft election, schema init, gossip).
    # Limit to 1:1 ratio of total nodes to CPUs to avoid thundering herd.
    max_by_cpu = max(1, nr_cpus // NODES_PER_WORKER)

    result = min(max_by_node_mem, max_by_cpu)
    reasoning = (f"min({max_by_node_mem} by memory, "
                 f"{max_by_cpu} by CPU/{NODES_PER_WORKER}-nodes-per-worker)")
    return (max(1, result), reasoning)


# --- IP allocation ---

def worker_ips(worker_id: int) -> list[str]:
    """Generate 3 unique IPs for a worker's cluster nodes.

    Uses 127.{worker_id+1}.1.{node_id} scheme. Each worker gets a
    unique second octet, so there are no collisions between workers.
    All 127.0.0.0/8 addresses are routable on Linux loopback.
    """
    second_octet = worker_id + 1
    assert 1 <= second_octet <= 254, f"worker_id {worker_id} out of range"
    return [f"127.{second_octet}.1.{node_id}" for node_id in range(1, NODES_PER_WORKER + 1)]


# --- Cgroup management ---

def setup_config_cgroup(config_name: str, controllers: list[str],
                        num_workers: int) -> Optional[Path]:
    """Create parent cgroup for a benchmark configuration.

    Returns the parent cgroup path, or None if controllers unavailable.
    Called from main process before spawning workers.
    """
    if not controllers:
        # Baseline: no cgroup needed
        return None

    # Check controllers available at CGROUP_TESTS level
    try:
        with open(CGROUP_TESTS / "cgroup.controllers", "r") as f:
            available = f.readline().split()
    except (FileNotFoundError, PermissionError):
        return None

    for ctrl in controllers:
        if ctrl not in available:
            print(f"  WARNING: controller '{ctrl}' not available, "
                  f"skipping config '{config_name}'")
            return None

    # Enable required controllers on CGROUP_TESTS subtree_control
    ctrl_str = " ".join(f"+{c}" for c in controllers)
    try:
        with open(CGROUP_TESTS / "cgroup.subtree_control", "w") as f:
            f.write(ctrl_str)
    except OSError as e:
        print(f"  WARNING: cannot enable controllers ({ctrl_str}): {e}")
        return None

    # Create parent cgroup for this config
    parent = CGROUP_TESTS / f"bench_{config_name}"
    parent.mkdir(exist_ok=True)

    # Enable controllers in parent's subtree_control so worker leaves get them
    try:
        with open(parent / "cgroup.subtree_control", "w") as f:
            f.write(ctrl_str)
    except OSError as e:
        print(f"  WARNING: cannot propagate controllers to parent: {e}")
        return None

    # Pre-create worker cgroup directories (workers will create leaves)
    for i in range(num_workers):
        worker_dir = parent / f"worker_{i}"
        worker_dir.mkdir(exist_ok=True)
        # Enable controllers in worker subtree so leaf gets them
        try:
            with open(worker_dir / "cgroup.subtree_control", "w") as f:
                f.write(ctrl_str)
        except OSError:
            pass
        leaf = worker_dir / "default"
        leaf.mkdir(exist_ok=True)

    return parent


def teardown_config_cgroup(config_name: str, num_workers: int) -> None:
    """Remove cgroup hierarchy for a benchmark configuration."""
    parent = CGROUP_TESTS / f"bench_{config_name}"
    if not parent.exists():
        return

    for i in range(num_workers):
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


# --- Scylla management ---

def make_scylla_yaml(workdir: Path, host: str, seeds: list[str]) -> dict[str, Any]:
    """Generate minimal scylla.yaml for a benchmark node."""
    return {
        "cluster_name": "bench_cgroup",
        "workdir": str(workdir.resolve()),
        "listen_address": host,
        "rpc_address": host,
        "api_address": host,
        "prometheus_address": host,
        "alternator_address": host,
        "seed_provider": [{
            "class_name":
                "org.apache.cassandra.locator.SimpleSeedProvider",
            "parameters": [{"seeds": ",".join(seeds)}],
        }],
        "developer_mode": True,
        "skip_wait_for_gossip_to_settle": 0,
        "shutdown_announce_in_ms": 0,
        "ring_delay_ms": 0,
        "num_tokens": 16,
        "flush_schema_tables_after_modification": False,
        "auto_snapshot": False,
        "range_request_timeout_in_ms": 60000,
        "read_request_timeout_in_ms": 60000,
        "write_request_timeout_in_ms": 60000,
        "request_timeout_in_ms": 60000,
    }


async def start_scylla_node(scylla_path: Path, workdir: Path,
                            host: str, seeds: list[str]) -> asyncio.subprocess.Process:
    """Start a single Scylla node."""
    workdir.mkdir(parents=True, exist_ok=True)
    conf_dir = workdir / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)

    # Write scylla.yaml
    config = make_scylla_yaml(workdir, host, seeds)
    with open(conf_dir / "scylla.yaml", "w") as f:
        yaml.dump(config, f)

    # Build command line
    cmdline = [str(scylla_path.resolve())] + SCYLLA_CMDLINE_OPTIONS + [
        "--api-address", host,
        "--api-port", str(API_PORT),
    ]

    # Clean environment
    env = os.environ.copy()
    env.pop("SCYLLA_HOME", None)

    # Open log file
    log_file = open(workdir / "scylla.log", "wb")

    proc = await asyncio.create_subprocess_exec(
        *cmdline,
        cwd=workdir,
        stdout=log_file,
        stderr=log_file,
        env=env,
    )
    proc._log_file = log_file  # type: ignore[attr-defined]
    proc._workdir = workdir  # type: ignore[attr-defined]
    return proc


async def wait_for_cql(host: str, port: int,
                       proc: asyncio.subprocess.Process,
                       timeout: float = 180.0) -> None:
    """Wait until Scylla CQL port accepts connections and responds."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if proc.returncode is not None:
            workdir = getattr(proc, "_workdir", None)
            log_tail = ""
            if workdir:
                log_path = workdir / "scylla.log"
                if log_path.exists():
                    lines = log_path.read_text(errors="replace").splitlines()
                    tail = lines[-50:] if len(lines) > 50 else lines
                    log_tail = "\n    ".join(tail)
            raise RuntimeError(
                f"Scylla process at {host} exited with code {proc.returncode}\n"
                f"    {log_tail}")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect((host, port))
            sock.close()
            # Try actual CQL query
            profile = ExecutionProfile(
                load_balancing_policy=RoundRobinPolicy(),
                request_timeout=5.0,
            )
            cluster = CqlCluster(
                contact_points=[host], port=port,
                protocol_version=4,
                connect_timeout=5.0,
                execution_profiles={EXEC_PROFILE_DEFAULT: profile},
            )
            try:
                session = cluster.connect()
                session.execute("SELECT release_version FROM system.local")
                session.shutdown()
                cluster.shutdown()
                return
            except Exception:
                cluster.shutdown()
        except (ConnectionRefusedError, OSError, NoHostAvailable):
            pass
        await asyncio.sleep(0.5)

    workdir = getattr(proc, "_workdir", None)
    log_tail = ""
    if workdir:
        log_path = workdir / "scylla.log"
        if log_path.exists():
            lines = log_path.read_text(errors="replace").splitlines()
            tail = lines[-50:] if len(lines) > 50 else lines
            log_tail = "\n    ".join(tail)
    raise TimeoutError(
        f"Scylla at {host} did not become ready within {timeout}s\n"
        f"    {log_tail}")


async def stop_scylla(proc: asyncio.subprocess.Process,
                      timeout: float = 30.0) -> None:
    """Gracefully stop Scylla via SIGTERM."""
    if proc.returncode is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


# --- Workload ---

def run_workload(host: str, port: int, num_rows: int,
                 row_size: int, batch_size: int = 50) -> tuple[float, float, float, float, float, list[float]]:
    """Run INSERT + SCAN workload, return timing data.

    Returns (insert_time, scan_time, total_time, insert_ops, scan_ops, batch_latencies_ms).
    """
    profile = ExecutionProfile(
        load_balancing_policy=RoundRobinPolicy(),
        request_timeout=60.0,
    )
    cluster = CqlCluster(
        contact_points=[host], port=port,
        protocol_version=4,
        connect_timeout=30.0,
        execution_profiles={EXEC_PROFILE_DEFAULT: profile},
    )
    session = cluster.connect()

    # Setup schema (RF=1 to isolate I/O to single node)
    session.execute(
        "CREATE KEYSPACE IF NOT EXISTS bench "
        "WITH replication = {'class': 'SimpleStrategy', "
        "'replication_factor': 1}")
    session.execute("USE bench")
    session.execute("DROP TABLE IF EXISTS data")
    session.execute(
        "CREATE TABLE data (pk int PRIMARY KEY, v blob)")

    # Prepare INSERT statement
    insert_stmt = session.prepare(
        "INSERT INTO data (pk, v) VALUES (?, ?)")

    # Generate random value
    random_value = os.urandom(row_size)

    # --- INSERT phase ---
    batch_latencies_ms: list[float] = []
    insert_start = time.perf_counter()

    row = 0
    while row < num_rows:
        batch = BatchStatement(batch_type=BatchType.UNLOGGED)
        batch_end = min(row + batch_size, num_rows)
        for i in range(row, batch_end):
            batch.add(insert_stmt, (i, random_value))

        batch_start = time.perf_counter()
        session.execute(batch)
        batch_elapsed = (time.perf_counter() - batch_start) * 1000.0
        batch_latencies_ms.append(batch_elapsed)

        row = batch_end

    insert_time = time.perf_counter() - insert_start

    # --- SCAN phase ---
    scan_start = time.perf_counter()
    result = session.execute(
        SimpleStatement("SELECT pk, v FROM data", fetch_size=5000))
    scan_count = 0
    for _ in result:
        scan_count += 1
    scan_time = time.perf_counter() - scan_start

    total_time = insert_time + scan_time
    insert_ops = num_rows / insert_time if insert_time > 0 else 0
    scan_ops = scan_count / scan_time if scan_time > 0 else 0

    session.shutdown()
    cluster.shutdown()

    return (insert_time, scan_time, total_time, insert_ops, scan_ops, batch_latencies_ms)


# --- Worker process ---

def worker_main(worker_id: int, scylla_path: str, config_name: str,
                cgroup_parent: Optional[str], enable_monitoring: bool,
                metrics_dir: str, num_rows: int, row_size: int,
                result_queue: multiprocessing.Queue) -> None:
    """Entry point for each worker subprocess.

    Starts a 3-node Scylla cluster, runs workload, puts result in queue.
    """
    try:
        result = asyncio.run(
            worker_async(worker_id, Path(scylla_path), config_name,
                         Path(cgroup_parent) if cgroup_parent else None,
                         enable_monitoring, Path(metrics_dir),
                         num_rows, row_size))
        result_queue.put(("ok", worker_id, result))
    except Exception as e:
        tb = traceback.format_exc()
        result_queue.put(("error", worker_id, f"{e}\n{tb}"))


async def worker_async(worker_id: int, scylla_path: Path,
                       config_name: str, cgroup_parent: Optional[Path],
                       enable_monitoring: bool, metrics_dir: Path,
                       num_rows: int, row_size: int) -> IterationResult:
    """Async worker: starts 3-node cluster, runs workload, returns result."""
    ips = worker_ips(worker_id)
    seed_ip = ips[0]

    # Create temp workdirs for each node
    base_dir = Path(tempfile.mkdtemp(prefix=f"bench_w{worker_id}_"))
    node_workdirs = [base_dir / f"node_{i}" for i in range(NODES_PER_WORKER)]

    procs: list[asyncio.subprocess.Process] = []
    gatherer: Optional[ResourceGatherOn] = None

    try:
        # Start seed node first
        proc0 = await start_scylla_node(
            scylla_path, node_workdirs[0], ips[0], seeds=[seed_ip])
        procs.append(proc0)

        # Start remaining nodes (seed = first node)
        for i in range(1, NODES_PER_WORKER):
            proc = await start_scylla_node(
                scylla_path, node_workdirs[i], ips[i], seeds=[seed_ip])
            procs.append(proc)

        # Move all node PIDs into cgroup leaf (if applicable)
        if cgroup_parent is not None:
            leaf = cgroup_parent / f"worker_{worker_id}" / "default"
            for proc in procs:
                try:
                    with open(leaf / "cgroup.procs", "w") as f:
                        f.write(str(proc.pid))
                except OSError as e:
                    raise RuntimeError(
                        f"Worker {worker_id}: cannot move PID {proc.pid} "
                        f"to cgroup {leaf}: {e}")

        # Start monitoring (if applicable)
        if enable_monitoring and cgroup_parent is not None:
            worker_cgroup = cgroup_parent / f"worker_{worker_id}"
            fake_test = SimpleNamespace(
                suite=SimpleNamespace(
                    suite_path=TOP_SRC_DIR / "test/pylib",
                    test_file_name="bench_cgroup_overhead.py",
                ),
                mode="dev",
                id=int(time.time() * 1000) + worker_id,
                shortname=f"bench_{config_name}_w{worker_id}",
                time_start=time.time(),
                time_end=0.0,
                success=True,
            )
            worker_name = f"bench_{config_name}_w{worker_id}"
            gatherer = ResourceGatherOn(metrics_dir, fake_test, worker_id=worker_name)
            gatherer.setup_test_tracking()
            gatherer.cgroup_monitor()

        # Wait for CQL on all nodes
        await asyncio.gather(*(
            wait_for_cql(ips[i], CQL_PORT, procs[i], timeout=300.0)
            for i in range(NODES_PER_WORKER)
        ))

        # Run workload against seed node (RF=1)
        loop = asyncio.get_event_loop()
        (insert_time, scan_time, total_time,
         insert_ops, scan_ops, batch_latencies) = await loop.run_in_executor(
            None, run_workload, seed_ip, CQL_PORT, num_rows, row_size)

        return IterationResult(
            worker_id=worker_id,
            insert_time=insert_time,
            scan_time=scan_time,
            total_time=total_time,
            insert_ops=insert_ops,
            scan_ops=scan_ops,
            batch_latencies_ms=batch_latencies,
        )

    finally:
        # Stop monitoring
        if gatherer is not None:
            gatherer.stop_monitoring()
            gatherer.teardown_test_tracking()

        # Stop all Scylla nodes
        for proc in procs:
            await stop_scylla(proc)

        # Close log files
        for proc in procs:
            log_file = getattr(proc, "_log_file", None)
            if log_file:
                log_file.close()

        # Cleanup workdirs
        shutil.rmtree(base_dir, ignore_errors=True)


# --- Main benchmark orchestrator ---

CONFIGURATIONS = [
    # (name, controllers, enable_monitoring)
    ("baseline", [], False),
    ("memory_monitored", ["memory"], True),
    ("all_monitored", ["memory", "cpu", "io", "pids"], True),
]


def run_parallel_iteration(num_workers: int, scylla_path: Path,
                           config_name: str, cgroup_parent: Optional[Path],
                           enable_monitoring: bool, metrics_dir: Path,
                           num_rows: int, row_size: int,
                           iter_timeout: int) -> ParallelIterationResult:
    """Launch N workers in parallel, wait for all, return aggregated result."""
    result_queue: multiprocessing.Queue = multiprocessing.Queue()

    # Spawn all workers
    workers: list[multiprocessing.Process] = []
    wall_start = time.perf_counter()

    for i in range(num_workers):
        p = multiprocessing.Process(
            target=worker_main,
            args=(i, str(scylla_path), config_name,
                  str(cgroup_parent) if cgroup_parent else None,
                  enable_monitoring, str(metrics_dir),
                  num_rows, row_size, result_queue),
            daemon=True,
        )
        p.start()
        workers.append(p)

    # Wait for all workers to finish (with timeout to prevent infinite hang)
    deadline = time.perf_counter() + iter_timeout
    for p in workers:
        remaining = max(0, deadline - time.perf_counter())
        p.join(timeout=remaining)
        if p.is_alive():
            print(f"\n    Worker {p.name} hung (>{iter_timeout}s), killing...",
                  flush=True)
            p.kill()
            p.join(timeout=10)

    wall_clock = time.perf_counter() - wall_start

    # Collect results from queue
    iter_result = ParallelIterationResult(wall_clock=wall_clock)

    while not result_queue.empty():
        msg = result_queue.get_nowait()
        if msg[0] == "ok":
            _, wid, worker_result = msg
            iter_result.worker_results.append(worker_result)
        else:
            _, wid, error_msg = msg
            print(f"\n    Worker {wid} FAILED: {error_msg}", flush=True)
            iter_result.failed_workers += 1

    return iter_result


def run_benchmark(args: argparse.Namespace) -> None:
    """Run the full benchmark across all configurations."""
    scylla_path = Path(args.scylla_path)
    if not scylla_path.exists():
        print(f"ERROR: Scylla binary not found: {scylla_path}",
              file=sys.stderr)
        sys.exit(1)

    num_workers, reasoning = compute_num_workers(args.workers)
    num_iterations = args.iterations
    num_rows = args.rows
    row_size = args.row_size
    iter_timeout = args.iter_timeout
    total_nodes = num_workers * NODES_PER_WORKER
    total_mem_gb = (total_nodes * MEMORY_PER_NODE) / (1024**3)

    print("Scylla cgroup overhead benchmark (parallel)")
    print("=" * 60)
    print(f"Binary:     {scylla_path.resolve()}")
    print(f"Workers:    {num_workers} ({reasoning})")
    print(f"Nodes:      {total_nodes} ({num_workers} workers x "
          f"{NODES_PER_WORKER} nodes)")
    print(f"Memory:     {total_mem_gb:.0f} GiB total "
          f"({NODES_PER_WORKER} x 1G per worker)")
    print(f"Options:    --smp 1, -m 1G, RF=1, --kernel-page-cache 1")
    print(f"Workload:   {num_rows} rows x {row_size} bytes per worker")
    print(f"Iterations: {num_iterations} (+ 1 warmup, discarded)")
    print(f"Timeout:    {iter_timeout}s per iteration")
    print()

    # Validate memory budget
    sys_mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    sys_mem_gb = sys_mem / (1024**3)
    if total_mem_gb > sys_mem_gb * 0.85:
        print(f"WARNING: Total node memory ({total_mem_gb:.0f} GiB) exceeds "
              f"85% of system memory ({sys_mem_gb:.0f} GiB)")
        print(f"Consider reducing --workers")
        print()

    # Set up cgroup hierarchy (Docker remount, process migration, +memory).
    setup_cgroup(True)

    # Temp dir for ResourceGatherOn SQLite DB
    metrics_dir = Path(tempfile.mkdtemp(prefix="bench_metrics_"))

    # Write host_info record (required FK for test records in SQLite)
    writer = SQLiteWriter(metrics_dir / DEFAULT_DB_NAME)
    writer.write_row_if_not_exist(
        gather_host_info(), HOST_INFO_TABLE, id_column="host_id")
    writer.close()

    results: list[ConfigResult] = []

    for config_name, controllers, monitoring in CONFIGURATIONS:
        print(f"Running: {config_name} ({num_workers} workers x "
              f"{NODES_PER_WORKER} nodes) ...", flush=True)

        # Set up config-level cgroup
        cgroup_parent = setup_config_cgroup(
            config_name, controllers, num_workers)
        if controllers and cgroup_parent is None:
            print(f"  SKIPPED (controllers not available)")
            print()
            continue

        config_result = ConfigResult(name=config_name, num_workers=num_workers)

        try:
            for i in range(num_iterations + 1):  # +1 for warmup
                is_warmup = (i == 0)
                label = "warmup" if is_warmup else f"iter {i}/{num_iterations}"
                print(f"  [{label}] ", end="", flush=True)

                iter_result = run_parallel_iteration(
                    num_workers, scylla_path, config_name,
                    cgroup_parent, monitoring, metrics_dir,
                    num_rows, row_size, iter_timeout)

                if is_warmup:
                    print(f"done (discarded) - wall: {iter_result.wall_clock:.1f}s, "
                          f"mean worker: {iter_result.mean_worker_time:.1f}s"
                          f"{f', {iter_result.failed_workers} failed' if iter_result.failed_workers else ''}")
                else:
                    print(f"done - wall: {iter_result.wall_clock:.1f}s, "
                          f"mean worker: {iter_result.mean_worker_time:.1f}s"
                          f"{f', {iter_result.failed_workers} failed' if iter_result.failed_workers else ''}")
                    if iter_result.worker_results:
                        config_result.iterations.append(iter_result)

        finally:
            teardown_config_cgroup(config_name, num_workers)

        if config_result.iterations:
            results.append(config_result)

        print()

    # --- Print results ---
    if not results:
        print("ERROR: No results collected.", file=sys.stderr)
        sys.exit(1)

    print_results(results)

    # Cleanup metrics temp dir
    shutil.rmtree(metrics_dir, ignore_errors=True)


def print_results(results: list[ConfigResult]) -> None:
    """Print formatted benchmark results."""
    baseline_wall = results[0].mean_wall_clock

    print()
    print("Results (mean of measured iterations)")
    print("=" * 100)
    header = (f"{'Configuration':<20} | {'Workers':>7} | {'Wall (s)':>9} | "
              f"{'W.stddev':>8} | {'Worker (s)':>10} | "
              f"{'Insert/s':>9} | {'Scan/s':>9} | {'Overhead':>8}")
    print(header)
    print("-" * 100)

    for r in results:
        if r.name == results[0].name:
            overhead = "baseline"
        else:
            pct = ((r.mean_wall_clock - baseline_wall) / baseline_wall) * 100.0
            sign = "+" if pct >= 0 else ""
            overhead = f"{sign}{pct:.1f}%"

        print(f"{r.name:<20} | {r.num_workers:>7} | "
              f"{r.mean_wall_clock:>9.2f} | {r.stddev_wall_clock:>8.2f} | "
              f"{r.mean_worker_time:>10.2f} | "
              f"{r.mean_insert_ops:>9.0f} | {r.mean_scan_ops:>9.0f} | "
              f"{overhead:>8}")

    print("-" * 100)
    print()

    # Statistical significance summary
    if len(results) >= 2:
        baseline = results[0]
        print("Statistical significance (2-sigma test on wall-clock time):")
        for r in results[1:]:
            pct = ((r.mean_wall_clock - baseline.mean_wall_clock)
                   / baseline.mean_wall_clock) * 100.0
            sign = "+" if pct >= 0 else ""
            combined_stddev = math.sqrt(
                baseline.stddev_wall_clock**2 + r.stddev_wall_clock**2)
            diff = abs(r.mean_wall_clock - baseline.mean_wall_clock)
            significant = (diff > 2 * combined_stddev
                           if combined_stddev > 0 else False)
            sig_label = ("SIGNIFICANT" if significant
                         else "within noise")
            print(f"  {r.name} vs baseline: "
                  f"{sign}{pct:.2f}% ({sig_label})")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark cgroup controller overhead under "
                    "parallel Scylla workload")
    parser.add_argument(
        "--scylla-path", type=str, default="build/dev/scylla",
        help="Path to Scylla binary (default: build/dev/scylla)")
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Number of parallel workers (default: auto from CPU/memory)")
    parser.add_argument(
        "--iterations", type=int, default=3,
        help="Number of measured iterations per config (default: 3)")
    parser.add_argument(
        "--rows", type=int, default=50000,
        help="Number of rows to insert per worker (default: 50000)")
    parser.add_argument(
        "--row-size", type=int, default=1024,
        help="Size of each row value in bytes (default: 1024)")
    parser.add_argument(
        "--iter-timeout", type=int, default=600,
        help="Timeout in seconds per iteration (default: 600)")
    args = parser.parse_args()

    run_benchmark(args)


if __name__ == "__main__":
    main()
