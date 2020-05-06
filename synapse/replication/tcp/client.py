# -*- coding: utf-8 -*-
# Copyright 2017 Vector Creations Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A replication client for use by synapse workers.
"""

import logging
from typing import TYPE_CHECKING

from twisted.internet.protocol import ReconnectingClientFactory

from synapse.api.constants import EventTypes
from synapse.replication.tcp.protocol import ClientReplicationStreamProtocol
from synapse.replication.tcp.streams.events import (
    EventsStream,
    EventsStreamEventRow,
    EventsStreamRow,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.replication.tcp.handler import ReplicationCommandHandler

logger = logging.getLogger(__name__)


class DirectTcpReplicationClientFactory(ReconnectingClientFactory):
    """Factory for building connections to the master. Will reconnect if the
    connection is lost.

    Accepts a handler that is passed to `ClientReplicationStreamProtocol`.
    """

    initialDelay = 0.1
    maxDelay = 1  # Try at least once every N seconds

    def __init__(
        self,
        hs: "HomeServer",
        client_name: str,
        command_handler: "ReplicationCommandHandler",
    ):
        self.client_name = client_name
        self.command_handler = command_handler
        self.server_name = hs.config.server_name
        self.hs = hs
        self._clock = hs.get_clock()  # As self.clock is defined in super class

        hs.get_reactor().addSystemEventTrigger("before", "shutdown", self.stopTrying)

    def startedConnecting(self, connector):
        logger.info("Connecting to replication: %r", connector.getDestination())

    def buildProtocol(self, addr):
        logger.info("Connected to replication: %r", addr)
        return ClientReplicationStreamProtocol(
            self.hs,
            self.client_name,
            self.server_name,
            self._clock,
            self.command_handler,
        )

    def clientConnectionLost(self, connector, reason):
        logger.error("Lost replication conn: %r", reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        logger.error("Failed to connect to replication: %r", reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)


class ReplicationDataHandler:
    """Handles incoming stream updates from replication.

    This instance notifies the slave data store about updates. Can be subclassed
    to handle updates in additional ways.
    """

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastore()
        self.pusher_pool = hs.get_pusherpool()
        self.notifier = hs.get_notifier()

    async def on_rdata(
        self, stream_name: str, instance_name: str, token: int, rows: list
    ):
        """Called to handle a batch of replication data with a given stream token.

        By default this just pokes the slave store. Can be overridden in subclasses to
        handle more.

        Args:
            stream_name: name of the replication stream for this batch of rows
            instance_name: the instance that wrote the rows.
            token: stream token for this batch of rows
            rows: a list of Stream.ROW_TYPE objects as returned by Stream.parse_row.
        """
        self.store.process_replication_rows(stream_name, instance_name, token, rows)

        if stream_name == EventsStream.NAME:
            # We shouldn't get multiple rows per token for events stream, so
            # we don't need to optimise this for multiple rows.
            for row in rows:
                if row.type != EventsStreamEventRow.TypeId:
                    continue
                assert isinstance(row, EventsStreamRow)

                event = await self.store.get_event(
                    row.data.event_id, allow_rejected=True
                )
                if event.rejected_reason:
                    continue

                extra_users = ()
                if event.type == EventTypes.Member:
                    extra_users = (event.state_key,)
                max_token = self.store.get_room_max_stream_ordering()
                self.notifier.on_new_room_event(event, token, max_token, extra_users)

            await self.pusher_pool.on_new_notifications(token, token)

    async def on_position(self, stream_name: str, instance_name: str, token: int):
        self.store.process_replication_rows(stream_name, instance_name, token, [])

    def on_remote_server_up(self, server: str):
        """Called when get a new REMOTE_SERVER_UP command."""
