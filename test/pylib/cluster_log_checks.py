#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#
"""Error, backtrace and core file checks over the server logs of a test's cluster."""

import pathlib
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import allure

from test.pylib.internal_types import ServerInfo, ServerNum
from test.pylib.log_browsing import ScyllaLogFile

if TYPE_CHECKING:
    from test.pylib.scylla_cluster import ScyllaCluster


class ClusterLogChecksMixin:
    """Reading and checking the server logs of the cluster."""

    async def check_all_errors(self: ScyllaCluster,
                               all_errors: bool = False) -> dict[ServerInfo, dict[str, list[str] | str]]:
        errors = defaultdict(dict)
        # find errors in logs
        for server in await self.all_servers():
            log_file = await self.server_open_log(server_id=server.server_id)
            # check if we should ignore cores on this server
            ignore_cores = []
            if self.ignore_cores_log_patterns:
                if matches := await log_file.grep("|".join(f"({p})" for p in set(self.ignore_cores_log_patterns))):
                    self.logger.debug("Will ignore cores on %s. Found the following log messages: %s", server, matches)
                    ignore_cores.append(server)
            critical_error_pattern = r"Assertion.*failed|AddressSanitizer"
            if server not in ignore_cores:
                critical_error_pattern += "|Aborting on shard"
            if found_critical := await log_file.grep(critical_error_pattern):
                errors[server]["critical"] = [e[0] for e in found_critical]
                # Find the backtraces for the critical errors
                if found_backtraces := await log_file.find_backtraces():
                    errors[server]["backtraces"] = found_backtraces
            if all_errors:
                if found_errors := await log_file.grep_for_errors(distinct_errors=True):
                    if filtered_errors := await self.filter_errors(found_errors):
                        errors[server]["error"] = filtered_errors
        # find core files
        for server, cores in (await self.find_cores()).items():
            errors[server]["cores"] = cores
        # add log file path to the report for servers that had errors or cores
        for server in await self.all_servers():
            log_file = await self.server_open_log(server_id=server.server_id)
            if server in errors:
                errors[server]["log"] = log_file.file.name

        return errors

    async def filter_errors(self: ScyllaCluster,
                            errors: list[str] | list[list[str]]) -> list[str] | list[list[str]]:
        exclude_errors_pattern = re.compile("|".join(f"{p}" for p in {
            *self.ignore_log_patterns,
            *self.ignore_cores_log_patterns,

            r"Compaction for .* deliberately stopped",
            r"update compaction history failed:.*ignored",

            # We may stop nodes that have not finished starting yet.
            r"(Startup|start) failed:.*(seastar::sleep_aborted|raft::request_aborted)",
            r"Timer callback failed: seastar::gate_closed_exception",

            # Ignore expected RPC errors when nodes are stopped.
            r"rpc - client .*(connection dropped|fail to connect)",

            # We see benign RPC errors when nodes start/stop.
            # If they cause system malfunction, it should be detected using higher-level tests.
            r"rpc::unknown_verb_error",
            r"raft_rpc - Failed to send",
            r"raft_topology.*(seastar::broken_promise|rpc::closed_error)",

            # Expected tablet migration stream failure where a node is stopped.
            # Refs: https://github.com/scylladb/scylladb/issues/19640
            r"Failed to handle STREAM_MUTATION_FRAGMENTS.*rpc::stream_closed",

            # Expected Raft errors on decommission-abort or node restart with MV.
            r"raft_topology - raft_topology_cmd.*failed with: raft::request_aborted",
        }))
        # Support both list[str] (from distinct_errors=True) and
        # list[list[str]] (from distinct_errors=False), matching against
        # the first line of each error group.
        def match_line(e: str | list[str]) -> bool:
            line = e[0] if isinstance(e, list) else e
            return not exclude_errors_pattern.search(line)
        return [e for e in errors if match_line(e)]

    async def find_cores(self: ScyllaCluster) -> dict[ServerInfo, list[str]]:
        """Find core files on all servers."""

        # find *.core files in current dir
        cores = [str(core_file.absolute()) for core_file in pathlib.Path('.').glob('*.core')]
        server_cores = dict()
        # match core files to servers by pid
        for server in await self.all_servers():
            if found_cores := [core for core in cores if f".{server.pid}." in core]:
                server_cores[server] = found_cores
        return server_cores

    async def gather_related_logs(self: ScyllaCluster, failed_test_path_dir: Path) -> None:
        for server in await self.all_servers():
            log_file = await self.server_open_log(server_id=server.server_id)
            shutil.copyfile(log_file.file, failed_test_path_dir / f"{pathlib.Path(log_file.file).name}")
            allure.attach(log_file.file.read_bytes(), name=log_file.file.name, attachment_type=allure.attachment_type.TEXT)

    async def server_open_log(self: ScyllaCluster, server_id: ServerNum) -> ScyllaLogFile:
        log_filename = await self.server_get_log_filename(server_id)
        return ScyllaLogFile(self.thread_pool, log_filename)
