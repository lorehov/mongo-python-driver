# Copyright 2017-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Logical sessions for ordering sequential operations.

Requires MongoDB 3.6.

.. versionadded:: 3.6

Causally Consistent Reads
=========================

.. code-block:: python

  with client.start_session(causally_consistent_reads=True) as session:
      collection = client.db.collection
      collection.update_one({'_id': 1}, {'$set': {'x': 10}}, session=session)
      secondary_c = collection.with_options(
          read_preference=ReadPreference.SECONDARY)

      # A secondary read waits for replication of the write.
      secondary_c.find_one({'_id': 1}, session=session)

If `causally_consistent_reads` is True, read operations that use the session are
causally after previous read and write operations. Using a causally consistent
session, an application can read its own writes and is guaranteed monotonic
reads, even when reading from replica set secondaries.

Classes
=======
"""

import collections
import uuid

from bson.binary import Binary
from pymongo import monotonic
from pymongo.errors import InvalidOperation


class SessionOptions(object):
    """Options for a new :class:`ClientSession`.

    :Parameters:
      - `causally_consistent_reads` (optional): If True, read operations are
        causally ordered within the session.
    """
    def __init__(self, causally_consistent_reads=False):
        self._causally_consistent_reads = causally_consistent_reads

    @property
    def causally_consistent_reads(self):
        """Whether causally consistent reads are configured."""
        return self._causally_consistent_reads


class ClientSession(object):
    """A session for ordering sequential operations."""
    def __init__(self, client, server_session, options, authset):
        # A MongoClient, a _ServerSession, a SessionOptions, and a set.
        self._client = client
        self._server_session = server_session
        self._options = options
        self._authset = authset

    def end_session(self):
        """Finish this session.

        It is an error to use the session or any derived
        :class:`~pymongo.database.Database`,
        :class:`~pymongo.collection.Collection`, or
        :class:`~pymongo.cursor.Cursor` after the session has ended.
        """
        self._end_session(True)

    def _end_session(self, lock):
        if self._server_session is not None:
            self.client._return_server_session(self._server_session, lock)
            self._server_session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_session()

    @property
    def client(self):
        """The :class:`~pymongo.mongo_client.MongoClient` this session was
        created from.
        """
        return self._client

    @property
    def options(self):
        """The :class:`SessionOptions` this session was created with."""
        return self._options

    @property
    def session_id(self):
        """A BSON document, the opaque server session identifier."""
        if self._server_session is None:
            raise InvalidOperation("Cannot use ended session")

        return self._server_session.session_id

    @property
    def has_ended(self):
        """True if this session is finished."""
        return self._server_session is None

    def _use_lsid(self):
        # Internal function.
        if self._server_session is None:
            raise InvalidOperation("Cannot use ended session")

        return self._server_session.use_lsid()


class _ServerSession(object):
    def __init__(self):
        # Ensure id is type 4, regardless of CodecOptions.uuid_representation.
        self.session_id = {'id': Binary(uuid.uuid4().bytes, 4)}
        self.last_use = monotonic.time()

    def timed_out(self, session_timeout_minutes):
        idle_seconds = monotonic.time() - self.last_use

        # Timed out if we have less than a minute to live.
        return idle_seconds > (session_timeout_minutes - 1) * 60

    def use_lsid(self):
        self.last_use = monotonic.time()
        return self.session_id


class _ServerSessionPool(collections.deque):
    """Pool of _ServerSession objects.

    This class is not thread-safe, access it while holding the Topology lock.
    """
    def get_server_session(self, session_timeout_minutes):
        # Although the Driver Sessions Spec says we only clear stale sessions
        # in return_server_session, PyMongo can't take a lock when returning
        # sessions from a __del__ method (like in Cursor.__die), so it can't
        # clear stale sessions there. In case many sessions were returned via
        # __del__, check for stale sessions here too.
        self._clear_stale(session_timeout_minutes)

        # The most recently used sessions are on the left.
        while self:
            s = self.popleft()
            if not s.timed_out(session_timeout_minutes):
                return s

        return _ServerSession()

    def return_server_session(self, server_session, session_timeout_minutes):
        self._clear_stale(session_timeout_minutes)
        if not server_session.timed_out(session_timeout_minutes):
            self.appendleft(server_session)

    def return_server_session_no_lock(self, server_session):
        self.appendleft(server_session)

    def _clear_stale(self, session_timeout_minutes):
        # Clear stale sessions. The least recently used are on the right.
        while self:
            if self[-1].timed_out(session_timeout_minutes):
                self.pop()
            else:
                # The remaining sessions also haven't timed out.
                break
