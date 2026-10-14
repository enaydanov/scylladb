#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#
"""The CQL driver session of a test's cluster."""

import logging
import ssl
import uuid
from time import time
from typing import TYPE_CHECKING

from cassandra import ConsistencyLevel
from cassandra.auth import AuthProvider
from cassandra.cluster import (
    EXEC_PROFILE_DEFAULT,
    Cluster as CassandraCluster,
    ExecutionProfile,
    Session as CassandraSession,
)
from cassandra.connection import EndPoint
from cassandra.policies import (
    ExponentialReconnectionPolicy,
    LoadBalancingPolicy,
    RoundRobinPolicy,
    TokenAwarePolicy,
    WhiteListRoundRobinPolicy,
)

from test.pylib.driver_utils import safe_driver_shutdown
from test.pylib.internal_types import IPAddress, ServerInfo
from test.pylib.util import Host, wait_for_cql_and_get_hosts

if TYPE_CHECKING:
    from test.pylib.scylla_cluster import ScyllaCluster


conn_logger = logging.getLogger("conn_messages")
conn_logger.setLevel(logging.INFO)


class CustomConnection(CassandraCluster.connection_class):
    """Driver connection which logs every message, for debugging."""

    def send_msg(self, *args, **argv):
        conn_logger.debug("send_msg: (%s): %s %s", id(self), args, argv)
        return super().send_msg(*args, **argv)

    def process_msg(self, msg, protocol_version):
        conn_logger.debug("process_msg: (%s): %s", id(self), msg)
        return super().process_msg(msg, protocol_version)


class ClusterDriverMixin:
    """CQL connections to the cluster: the test's session and the ones built on demand."""

    def con_gen(self,
                hosts: list[IPAddress | EndPoint],
                port: int = 9042,
                use_ssl: bool = False,
                auth_provider: AuthProvider | None = None,
                load_balancing_policy: LoadBalancingPolicy = RoundRobinPolicy()) -> CassandraCluster:
        """Create a CQL Cluster connection object according to configuration.

        It does not .connect() yet.
        """
        assert hosts, "python driver connection needs at least one host to connect to"
        profile = ExecutionProfile(
            load_balancing_policy=load_balancing_policy,
            consistency_level=ConsistencyLevel.LOCAL_QUORUM,
            serial_consistency_level=ConsistencyLevel.LOCAL_SERIAL,
            # The default timeouts should have been more than enough, but in some
            # extreme cases with a very slow debug build running on a slow or very busy
            # machine, they may not be. Observed tests reach 160 seconds. So it's
            # incremented to 200 seconds.
            # See issue #11289.
            # NOTE: request_timeout is the main cause of timeouts, even if logs say heartbeat
            request_timeout=200,
        )
        whitelist_profile = ExecutionProfile(
            load_balancing_policy=TokenAwarePolicy(WhiteListRoundRobinPolicy(hosts)),
            consistency_level=ConsistencyLevel.LOCAL_QUORUM,
            serial_consistency_level=ConsistencyLevel.LOCAL_SERIAL,
            request_timeout=200,
        )
        return CassandraCluster(
            execution_profiles={
                EXEC_PROFILE_DEFAULT: profile,
                "whitelist": whitelist_profile,
            },
            contact_points=hosts,
            port=port,
            # TODO: make the protocol version an option, to allow testing with
            # different versions. If we drop this setting completely, it will
            # mean pick the latest version supported by the client and the server.
            protocol_version=4,
            # NOTE: No auth provider as auth keysppace has RF=1 and topology will take
            # down nodes, causing errors. If auth is needed in the future for topology
            # tests, they should bump up auth RF and run repair.
            ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if use_ssl else None,
            # The default timeouts should have been more than enough, but in some
            # extreme cases with a very slow debug build running on a slow or very busy
            # machine, they may not be. Observed tests reach 160 seconds. So it's
            # incremented to 200 seconds.
            # See issue #11289.
            connect_timeout=200,
            control_connection_timeout=200,
            # NOTE: max_schema_agreement_wait must be 2x or 3x smaller than request_timeout
            # else the driver can't handle a server being down
            max_schema_agreement_wait=20,
            idle_heartbeat_timeout=200,
            # The default reconnection policy has a large maximum interval
            # between retries (600 seconds). In tests that restart/replace nodes,
            # where a node can be unavailable for an extended period of time,
            # this can cause the reconnection retry interval to get very large,
            # longer than a test timeout.
            # The base delay decides how long a reconnect is delayed after a node is
            # already back up; max_attempts keeps the overall budget at ~251s.
            reconnection_policy=ExponentialReconnectionPolicy(0.1, 1.0, 250),
            auth_provider=auth_provider,
            # Capture messages for debugging purposes.
            connection_class=CustomConnection,
        )

    async def driver_connect(self: ScyllaCluster,
                             server: ServerInfo | None = None,
                             auth_provider: AuthProvider | None = None) -> None:
        """Connect to cluster."""

        targets = [server] if server else await self.running_servers()
        servers = [s_info.rpc_address for s_info in targets]
        # avoids leaking connections if driver wasn't closed before
        self.driver_close()
        self.logger.debug("driver connecting to %s", servers)
        self.ccluster = self.con_gen(
            servers,
            self.port,
            self.use_ssl,
            auth_provider if auth_provider else self.auth_provider,
            self.load_balancing_policy,
        )
        self.cql = self.ccluster.connect()

    def driver_close(self: ScyllaCluster) -> None:
        """Disconnect from cluster."""

        for cluster in self.exclusive_clusters:
            safe_driver_shutdown(cluster)
        self.exclusive_clusters.clear()
        if self.ccluster is not None:
            self.logger.debug("shutting down driver")
            safe_driver_shutdown(self.ccluster)
            self.ccluster = None
        self.cql = None

    def get_cql(self: ScyllaCluster) -> CassandraSession:
        """Precondition: driver is connected."""

        assert self.cql
        return self.cql

    async def get_ready_cql(self: ScyllaCluster,
                            servers: list[ServerInfo]) -> tuple[CassandraSession, list[Host]]:
        """Precondition: driver is connected."""

        cql = self.get_cql()
        await self.servers_see_each_other(servers)
        hosts = await wait_for_cql_and_get_hosts(cql, servers, time() + 60)
        return cql, hosts

    async def get_cql_exclusive(self: ScyllaCluster,
                                server: ServerInfo,
                                auth_provider: AuthProvider | None = None) -> CassandraSession:
        cluster = self.con_gen([server.ip_addr], self.port, self.use_ssl,
                               auth_provider if auth_provider else self.auth_provider,
                               WhiteListRoundRobinPolicy([server.ip_addr]))
        self.exclusive_clusters.append(cluster)
        cql = cluster.connect()
        await wait_for_cql_and_get_hosts(cql, [server], time() + 60)
        return cql

    def _driver_update(self: ScyllaCluster) -> None:
        if self.ccluster is not None:
            self.logger.debug("refresh driver node list")
            self.ccluster.control_connection.refresh_node_list_and_token_map()

    async def get_table_id(self: ScyllaCluster, keyspace: str, table: str) -> uuid.UUID:
        rows = await self.get_cql().run_async(
            f"select id from system_schema.tables where keyspace_name = '{keyspace}' and table_name = '{table}'"
        )
        return rows[0].id

    async def get_view_id(self: ScyllaCluster, keyspace: str, view: str) -> uuid.UUID:
        rows = await self.get_cql().run_async(
            f"select id from system_schema.views where keyspace_name = '{keyspace}' and view_name = '{view}'"
        )
        return rows[0].id

    async def get_table_or_view_id(self: ScyllaCluster, keyspace: str, table: str) -> uuid.UUID:
        rows = await self.get_cql().run_async(
            f"select id from system_schema.tables where keyspace_name = '{keyspace}' and table_name = '{table}'"
        )
        if not rows:
            rows = await self.get_cql().run_async(
                f"select id from system_schema.views where keyspace_name = '{keyspace}' and view_name = '{table}'"
            )
        return rows[0].id
