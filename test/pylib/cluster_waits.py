#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#
"""Waits for what the servers of a test's cluster see of each other."""

from time import time
from typing import TYPE_CHECKING

from test.pylib.internal_types import HostID, IPAddress, ServerInfo, ServerNum
from test.pylib.util import gather_safely, wait_for

if TYPE_CHECKING:
    from test.pylib.scylla_cluster import ScyllaCluster


class ClusterWaitsMixin:
    """Waiting until the servers agree on who is alive and known."""

    async def wait_for_host_known(self: ScyllaCluster,
                                  dst_server_ip: IPAddress,
                                  expect_host_id: HostID,
                                  deadline: float | None = None) -> None:
        """Waits until dst_server_id knows about expect_host_id, with timeout."""

        async def host_is_known() -> bool | None:
            host_id_map = await self.api.get_host_id_map(dst_server_ip)
            return True if any(entry for entry in host_id_map if entry["value"] == expect_host_id) else None

        await wait_for(host_is_known, deadline or (time() + 30))

    async def wait_for_scylla_process_status(self: ScyllaCluster,
                                             server_id: ServerNum,
                                             expected_statuses: list[str],
                                             deadline: float | None = None) -> str:
        """Wait for Scylla's process status for server_id will be as expected, with timeout."""

        async def process_status_is_as_expected() -> str | None:
            current_status = await self.server_get_process_status(server_id)
            if current_status in expected_statuses:
                return current_status
            return None

        return await wait_for(process_status_is_as_expected, deadline or (time() + 30))

    async def server_sees_others(self: ScyllaCluster,
                                 server_id: ServerNum,
                                 count: int,
                                 interval: float = 45.) -> None:
        """Wait till a server sees a minimum given count of other servers."""

        if count < 1:
            return
        server_ip = await self.get_host_ip(server_id)

        async def _sees_min_others() -> bool | None:
            alive_nodes = await self.api.get_alive_endpoints(server_ip)
            if len(alive_nodes) > count:
                return True
            return None

        await wait_for(_sees_min_others, time() + interval, period=.5)

    async def server_sees_other_server(self: ScyllaCluster,
                                       server_ip: IPAddress,
                                       other_ip: IPAddress,
                                       interval: float = 45.) -> None:
        """Wait till a server sees another specific server IP as alive."""

        async def _sees_another_server() -> bool | None:
            alive_nodes = await self.api.get_alive_endpoints(server_ip)
            if other_ip in alive_nodes:
                return True
            return None

        await wait_for(_sees_another_server, time() + interval, period=.5)

    async def servers_see_each_other(self: ScyllaCluster,
                                     servers: list[ServerInfo],
                                     interval: float = 45.) -> None:
        """Wait till all servers see all other servers in the list."""

        others = [self.server_sees_others(srv.server_id, len(servers) - 1, interval) for srv in servers]
        await gather_safely(*others)

    async def server_not_sees_other_server(self: ScyllaCluster,
                                           server_ip: IPAddress,
                                           other_ip: IPAddress,
                                           interval: float = 45.) -> None:
        """Wait till a server sees another specific server IP as dead."""

        async def _not_sees_another_server() -> bool | None:
            alive_nodes = await self.api.get_alive_endpoints(server_ip)
            if other_ip not in alive_nodes:
                return True
            return None

        await wait_for(_not_sees_another_server, time() + interval, period=.5)

    async def others_not_see_server(self: ScyllaCluster,
                                    server_ip: IPAddress,
                                    interval: float = 45.) -> None:
        """Wait till a server is seen as dead by all other running servers in the cluster."""

        others_ips = [srv.ip_addr for srv in await self.running_servers() if srv.ip_addr != server_ip]
        await gather_safely(*(self.server_not_sees_other_server(ip, server_ip, interval) for ip in others_ips))
