"""Current model activity from the dashboard's existing, read-only Gateway connection."""
import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass


RECHECK_SECONDS = 120
POLL_SECONDS = 15


def clock_ms():
    return time.time_ns() // 1_000_000


def agent_from_key(key):
    if isinstance(key, str):
        parts = key.split(':', 2)
        if len(parts) == 3 and parts[0] == 'agent' and parts[1]:
            return parts[1]
    return None


@dataclass(frozen=True)
class SessionRun:
    session_id: str
    state: str
    run_ids: tuple
    confirmed_at: int


@dataclass(frozen=True)
class Change:
    key: str
    session_id: str
    state: str
    run_ids: tuple
    run_id: str
    observed: int


def read_change(record, key, observed, *, listing=False):
    """Keep only run identity and state. A session may exist long after its run ended."""
    if not agent_from_key(key):
        return None
    active = record.get('hasActiveRun')
    status = record.get('status')
    if active is None and status not in ('queued', 'running') and record.get('reason') != 'delete':
        return None
    if active is False or record.get('reason') == 'delete':
        state = 'ended'
    elif active is True or (listing and status in ('queued', 'running')):
        state = 'waiting' if status == 'queued' else 'working'
    else:
        # Stored status without an active flag on an unsolicited event can be historical.
        return None
    ids = record.get('activeRunIds')
    return Change(key, record.get('sessionId') or '', state,
                  tuple(i for i in ids if isinstance(i, str)) if isinstance(ids, list) else (),
                  record.get('runId') or '', observed)


def apply_change(rows, change):
    old = rows.get(change.key)
    if change.state != 'ended':
        rows[change.key] = SessionRun(change.session_id, change.state, change.run_ids, change.observed)
    elif old:
        if change.session_id and old.session_id and old.session_id != change.session_id:
            return
        if change.run_id and old.run_ids:
            # A late finish from a different or overlapping run does not finish this session.
            if change.run_id not in old.run_ids:
                return
            remaining = tuple(i for i in old.run_ids if i != change.run_id)
            if remaining:
                rows[change.key] = SessionRun(old.session_id, old.state, remaining, old.confirmed_at)
                return
        rows.pop(change.key, None)


class ActivityFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = {}
        self._during_sync = None
        self._connected = False
        self._stale = True
        self._checked = 0
        self._revision = 0

    def connection(self, connected):
        with self._lock:
            self._connected = connected
            self._stale = True
            self._revision += 1

    def invalidate(self):
        with self._lock:
            self._stale = True
            self._revision += 1

    def due(self, now):
        with self._lock:
            return self._stale or now - self._checked >= RECHECK_SECONDS * 1000

    def event(self, frame):
        if frame.get('event') != 'sessions.changed':
            return
        payload = frame.get('payload')
        if not isinstance(payload, dict):
            return
        row = payload.get('session')
        # The Gateway can send a compact change instead of a whole session.
        state = {**row, **payload} if isinstance(row, dict) else payload
        change = read_change(state, payload.get('sessionKey') or state.get('key'), clock_ms())
        if change:
            with self._lock:
                apply_change(self._rows, change)
                if self._during_sync is not None:
                    self._during_sync.append(change)

    async def refresh(self, call):
        # This coroutine has a single scheduler. Events are still delivered by the WS reader.
        with self._lock:
            self._during_sync = []
            revision = self._revision
        try:
            rows = {}
            offset = 0
            seen_keys = set()
            total = None
            while True:
                page = await call('sessions.list', {
                    'source': 'activity', 'activeOnly': True, 'rowMode': 'compact',
                    'archived': 'all', 'excludeDock': True, 'limit': 100, 'offset': offset,
                })
                if not isinstance(page, dict) or not isinstance(page.get('sessions'), list):
                    raise ValueError('activity list has no sessions')
                page_total = page.get('totalCount')
                if type(page_total) is not int or page_total < 0:
                    raise ValueError('activity list has no valid filtered total')
                if total is not None and page_total != total:
                    raise ValueError('activity list changed during pagination')
                total = page_total
                checked = clock_ms()
                for record in page['sessions']:
                    if not isinstance(record, dict) or not isinstance(record.get('key'), str):
                        raise ValueError('activity list has invalid session identity')
                    key = record['key']
                    if key in seen_keys:
                        raise ValueError('activity list contains overlapping pages')
                    seen_keys.add(key)
                    change = read_change(record, key, checked, listing=True)
                    if change is None and agent_from_key(key):
                        raise ValueError('activity row has no current run state')
                    if change and change.state != 'ended':
                        apply_change(rows, change)
                if page.get('hasMore') is False:
                    # A selected session can disappear before the Gateway materializes its row.
                    if len(seen_keys) != total:
                        raise ValueError('activity list is missing selected rows')
                    break
                next_offset = page.get('nextOffset')
                if page.get('hasMore') is not True or type(next_offset) is not int or next_offset <= offset:
                    raise ValueError('activity list has invalid paging')
                offset = next_offset
            with self._lock:
                if self._revision != revision:
                    # A disconnect or event sequence gap invalidated this in-flight sample.
                    raise ConnectionError('activity stream changed during snapshot')
                for change in self._during_sync:
                    apply_change(rows, change)
                self._rows = rows
                self._checked = checked
                self._stale = False
        except BaseException:
            self.invalidate()
            raise
        finally:
            with self._lock:
                self._during_sync = None

    def snapshot(self, now):
        with self._lock:
            sessions = list(self._rows.items())
            checked, connected, stale = self._checked, self._connected, self._stale
        agents = {}
        for key, run in sessions:
            aid = agent_from_key(key)
            child = ':subagent:' in key
            state = run.state
            kind = 'queued' if state == 'waiting' else ('subagent' if child else 'self')
            entry = agents.setdefault(aid, {
                'state': state, 'kind': kind, 'confirmedAt': run.confirmed_at,
                'activeChildren': 0, 'activeChildKeys': [], 'activeSessions': {},
            })
            if state == 'working':
                entry['state'] = 'working'
                if child:
                    entry['activeChildKeys'].append(key)
                    entry['activeChildren'] += 1
                    if entry['kind'] == 'queued':
                        entry['kind'] = 'subagent'
                else:
                    entry['kind'] = 'self'
                entry['activeSessions'][key] = run.session_id
            entry['confirmedAt'] = max(entry['confirmedAt'], run.confirmed_at)
        return {'ready': bool(checked), 'connected': connected,
                'stale': stale or not connected or now - checked > (RECHECK_SECONDS + 2 * POLL_SECONDS) * 1000,
                'checkedAt': checked, 'agents': agents}


activity_feed = ActivityFeed()


class GatewayReader:
    """One socket reader. RPC calls from the activity and process samplers share it."""
    def __init__(self, ws, feed):
        self.ws = ws
        self.feed = feed
        self.pending = {}
        self._last_seq = None

    async def call(self, method, params):
        rid = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.ws.send(json.dumps({'type': 'req', 'method': method, 'id': rid, 'params': params}))
            return await asyncio.wait_for(future, timeout=10)
        finally:
            self.pending.pop(rid, None)

    async def listen(self):
        try:
            async for message in self.ws:
                frame = json.loads(message)
                if frame.get('type') == 'event':
                    seq = frame.get('seq')
                    if type(seq) is int:
                        if self._last_seq is not None and seq > self._last_seq + 1:
                            self.feed.invalidate()
                        self._last_seq = seq
                    self.feed.event(frame)
                elif frame.get('type') == 'res':
                    future = self.pending.get(frame.get('id'))
                    if future and not future.done():
                        if frame.get('ok') is True:
                            future.set_result(frame.get('payload'))
                        else:
                            future.set_exception(RuntimeError('Gateway declined read request'))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError('Gateway disconnected'))
        raise ConnectionError('Gateway socket closed')


class BackgroundSampler:
    """One sampler across connections, including cold start and Gateway outages."""
    def __init__(self, work):
        self.work = work
        self.call = None
        self.changed = asyncio.Event()

    def set_connection(self, call):
        if self.call != call:
            self.call = call
            if call is None:
                self.work.invalidate()
            self.changed.set()

    async def run(self):
        while True:
            self.changed.clear()
            try:
                await self.work.refresh(self.call, clock_ms())
            except Exception as exc:
                logging.warning('[WS] background status unavailable (%s)', type(exc).__name__)
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass


async def watch_connection(ws, token, feed, sampler):
    challenge = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if challenge.get('type') != 'event' or challenge.get('event') != 'connect.challenge':
        raise ConnectionError('Gateway challenge missing')
    conn = GatewayReader(ws, feed)
    reader = asyncio.create_task(conn.listen())
    tasks = [reader]
    try:
        await conn.call('connect', {
            'minProtocol': 3, 'maxProtocol': 4,
            'client': {'id': 'gateway-client', 'version': '1.0.0', 'platform': 'darwin', 'mode': 'backend'},
            'caps': ['tool-events'], 'auth': {'token': token}, 'role': 'operator', 'scopes': ['operator.read'],
        })
        await conn.call('sessions.subscribe', {})
        feed.connection(True)

        async def poll_models():
            while True:
                if feed.due(clock_ms()):
                    try:
                        await feed.refresh(conn.call)
                    except (RuntimeError, ValueError, TimeoutError, ConnectionError) as exc:
                        logging.warning('[WS] activity snapshot unavailable (%s)', type(exc).__name__)
                await asyncio.sleep(POLL_SECONDS)

        sampler.set_connection(conn.call)
        tasks.append(asyncio.create_task(poll_models()))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        feed.connection(False)
        sampler.set_connection(None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
