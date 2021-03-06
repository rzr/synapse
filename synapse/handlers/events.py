# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
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

from twisted.internet import defer

from synapse.util.logutils import log_function
from synapse.types import UserID
from synapse.events.utils import serialize_event

from ._base import BaseHandler

import logging
import random


logger = logging.getLogger(__name__)


class EventStreamHandler(BaseHandler):

    def __init__(self, hs):
        super(EventStreamHandler, self).__init__(hs)

        # Count of active streams per user
        self._streams_per_user = {}
        # Grace timers per user to delay the "stopped" signal
        self._stop_timer_per_user = {}

        self.distributor = hs.get_distributor()
        self.distributor.declare("started_user_eventstream")
        self.distributor.declare("stopped_user_eventstream")

        self.clock = hs.get_clock()

        self.notifier = hs.get_notifier()

    @defer.inlineCallbacks
    @log_function
    def get_stream(self, auth_user_id, pagin_config, timeout=0,
                   as_client_event=True, affect_presence=True,
                   only_room_events=False):
        """Fetches the events stream for a given user.

        If `only_room_events` is `True` only room events will be returned.
        """
        auth_user = UserID.from_string(auth_user_id)

        try:
            if affect_presence:
                if auth_user not in self._streams_per_user:
                    self._streams_per_user[auth_user] = 0
                    if auth_user in self._stop_timer_per_user:
                        try:
                            self.clock.cancel_call_later(
                                self._stop_timer_per_user.pop(auth_user)
                            )
                        except:
                            logger.exception("Failed to cancel event timer")
                    else:
                        yield self.distributor.fire(
                            "started_user_eventstream", auth_user
                        )
                self._streams_per_user[auth_user] += 1

            rm_handler = self.hs.get_handlers().room_member_handler

            app_service = yield self.store.get_app_service_by_user_id(
                auth_user.to_string()
            )
            if app_service:
                rooms = yield self.store.get_app_service_rooms(app_service)
                room_ids = set(r.room_id for r in rooms)
            else:
                room_ids = yield rm_handler.get_joined_rooms_for_user(auth_user)

            if timeout:
                # If they've set a timeout set a minimum limit.
                timeout = max(timeout, 500)

                # Add some randomness to this value to try and mitigate against
                # thundering herds on restart.
                timeout = random.randint(int(timeout*0.9), int(timeout*1.1))

            events, tokens = yield self.notifier.get_events_for(
                auth_user, room_ids, pagin_config, timeout,
                only_room_events=only_room_events
            )

            time_now = self.clock.time_msec()

            chunks = [
                serialize_event(e, time_now, as_client_event) for e in events
            ]

            chunk = {
                "chunk": chunks,
                "start": tokens[0].to_string(),
                "end": tokens[1].to_string(),
            }

            defer.returnValue(chunk)

        finally:
            if affect_presence:
                self._streams_per_user[auth_user] -= 1
                if not self._streams_per_user[auth_user]:
                    del self._streams_per_user[auth_user]

                    # 10 seconds of grace to allow the client to reconnect again
                    #   before we think they're gone
                    def _later():
                        logger.debug(
                            "_later stopped_user_eventstream %s", auth_user
                        )

                        self._stop_timer_per_user.pop(auth_user, None)

                        return self.distributor.fire(
                            "stopped_user_eventstream", auth_user
                        )

                    logger.debug("Scheduling _later: for %s", auth_user)
                    self._stop_timer_per_user[auth_user] = (
                        self.clock.call_later(30, _later)
                    )


class EventHandler(BaseHandler):

    @defer.inlineCallbacks
    def get_event(self, user, event_id):
        """Retrieve a single specified event.

        Args:
            user (synapse.types.UserID): The user requesting the event
            event_id (str): The event ID to obtain.
        Returns:
            dict: An event, or None if there is no event matching this ID.
        Raises:
            SynapseError if there was a problem retrieving this event, or
            AuthError if the user does not have the rights to inspect this
            event.
        """
        event = yield self.store.get_event(event_id)

        if not event:
            defer.returnValue(None)
            return

        if hasattr(event, "room_id"):
            yield self.auth.check_joined_room(event.room_id, user.to_string())

        defer.returnValue(event)
