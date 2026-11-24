import asyncio
import json
import sys
import unittest
from unittest.mock import Mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard_activity import ActivityFeed, BackgroundSampler, GatewayReader, clock_ms, watch_connection


PARENT = 'agent:jiweixia:telegram:direct:fixture'
CHILD = 'agent:jiweixia:subagent:fixture'
PEER = 'agent:duijiaoxia:subagent:fixture'


def listed(key, status='running', sid='s1', **extra):
    return dict(key=key, sessionId=sid, hasActiveRun=True, status=status, **extra)


def event(key, running, sid='s1', **extra):
    return {'type': 'event', 'event': 'sessions.changed',
            'payload': dict(sessionKey=key, sessionId=sid, hasActiveRun=running, **extra)}


def page(rows):
    return dict(sessions=rows, hasMore=False, nextOffset=None, totalCount=len(rows))


class FeedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.feed = ActivityFeed()
        self.feed.connection(True)

    async def load(self, rows):
        async def rpc(method, params):
            self.assertEqual(method,'sessions.list')
            self.assertEqual(params['source'], 'activity')
            self.assertTrue(params['activeOnly'])
            return page(rows)
        await self.feed.refresh(rpc)

    def state(self):
        return self.feed.snapshot(clock_ms())

    async def test_empty_list_queue_and_work(self):
        self.assertTrue(self.state()['stale'])
        await self.load([])
        self.assertFalse(self.state()['stale'])
        self.assertEqual(self.state()['agents'], {})
        await self.load([listed(PARENT, 'queued'), listed(PEER)])
        self.assertEqual(self.state()['agents']['jiweixia']['state'], 'waiting')
        self.assertEqual(self.state()['agents']['jiweixia']['activeChildren'], 0)
        self.assertEqual(self.state()['agents']['duijiaoxia']['kind'], 'subagent')

    async def test_paging_with_event_interleaving(self):
        calls = []
        async def rpc(method, params):
            calls.append(params['offset'])
            if len(calls) == 1:
                self.feed.event(event(PEER, False))
                return dict(sessions=[listed(CHILD, sid='s2')], hasMore=True, nextOffset=1, totalCount=2)
            self.feed.event(event(PARENT, True))
            return dict(page([listed(PEER)]), totalCount=2)
        await self.feed.refresh(rpc)
        self.assertEqual(calls, [0, 1])
        state = self.state()['agents']
        self.assertEqual(list(state), ['jiweixia'])
        self.assertEqual(state['jiweixia']['kind'], 'self')
        self.assertEqual(state['jiweixia']['activeChildKeys'], [CHILD])
        self.feed.event(event(PARENT, False))
        self.assertEqual(self.state()['agents']['jiweixia']['kind'], 'subagent')

    async def test_incomplete_query_keeps_previous_list_and_new_events(self):
        await self.load([listed(PARENT)])
        previous = self.state()['checkedAt']
        async def rpc(method, params):
            if params['offset'] == 0:
                self.feed.event(event(CHILD, True))
                return dict(sessions=[], hasMore=True, nextOffset=2,totalCount=3)
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.feed.refresh(rpc)
        self.assertTrue(self.state()['stale'])
        self.assertEqual(self.state()['checkedAt'], previous)
        self.assertEqual(set(self.state()['agents']['jiweixia']['activeSessions']), {CHILD,PARENT})
        await self.load([])
        self.assertFalse(self.state()['stale'])
        self.assertFalse(self.state()['agents'])

    async def test_invalid_shape_is_not_empty(self):
        for malformed in ({'result': page([])}, {'sessions':[]}, {'sessions':[], 'hasMore':True,'nextOffset':0},page([{}])):
            async def rpc(*unused):
                return malformed
            with self.assertRaises(ValueError):
                await self.feed.refresh(rpc)
            self.assertTrue(self.state()['stale'])

    async def test_unstable_pages_cannot_publish_partial_snapshot(self):
        await self.load([listed(PARENT)])
        for last in [dict(page([listed(CHILD)]), totalCount=2),
                     dict(page([listed(PEER)]), totalCount=3)]:
            async def rpc(method, params):
                return last if params["offset"] else dict(sessions=[listed(CHILD)], hasMore=True, nextOffset=1, totalCount=2)
            with self.assertRaises(ValueError):
                await self.feed.refresh(rpc)
            self.assertTrue(self.state()['stale'])
            self.assertEqual(self.state()['agents']['jiweixia']['kind'], 'self')

    async def test_missing_selected_rows_does_not_publish_idle(self):
        await self.load([listed(PARENT)])
        for incomplete in [dict(page([]), totalCount=1), dict(page([listed(CHILD)]), totalCount=2),
                           dict(page([listed(CHILD)]), totalCount=0), dict(page([]), totalCount=True),
                           {'sessions': [], 'hasMore': False}]:
            async def rpc(*unused):
                return incomplete
            with self.assertRaises(ValueError):
                await self.feed.refresh(rpc)
            self.assertTrue(self.state()['stale'])
            self.assertIn('jiweixia', self.state()['agents'])
        await self.load([])
        self.assertFalse(self.state()['stale'])
        self.assertFalse(self.state()['agents'])

    async def test_dual_run_and_late_terminal(self):
        await self.load([listed(CHILD, activeRunIds=['r1','r2']), listed(CHILD+'2')])
        self.feed.event(event(CHILD,False,sid='retired',runId='r2'))
        self.feed.event(event(CHILD,False,runId='older'))
        self.feed.event(event(CHILD,False,runId='r1'))
        self.assertEqual(self.state()['agents']['jiweixia']['activeChildren'],2)
        self.feed.event(event(CHILD,False,runId='r2'))
        self.assertEqual(self.state()['agents']['jiweixia']['activeChildren'],1)
        self.feed.event(event(CHILD+'2',False))
        self.feed.event(event(CHILD+'2',False))
        self.assertFalse(self.state()['agents'])

    async def test_quiet_run_refreshed_and_missed_events(self):
        await self.load([listed(CHILD)])
        before=self.state()['checkedAt']
        self.assertTrue(self.feed.due(before + 120_000))
        await self.load([listed(CHILD)])
        self.assertEqual(self.state()['agents']['jiweixia']['state'],'working')
        async def rpc(*unused):
            self.feed.invalidate()
            return page([])
        with self.assertRaises(ConnectionError):
            await self.feed.refresh(rpc)
        self.assertTrue(self.state()['stale'])
        self.assertIn('jiweixia',self.state()['agents'])
        self.feed.connection(False)
        self.assertFalse(self.state()['connected'])


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.requests = asyncio.Queue()

    async def recv(self):
        return json.dumps(await self.incoming.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def send(self, data):
        await self.requests.put(json.loads(data))

    async def respond(self, req, payload, ok=True):
        await self.incoming.put({'type':'res','id':req['id'],'ok':ok,'payload':payload})


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_subscription_does_not_continue(self):
        ws=Socket()
        await ws.incoming.put({'type':'event','event':'connect.challenge'})
        feed=ActivityFeed()
        task=asyncio.create_task(watch_connection(ws,'fixture',feed,BackgroundSampler(Mock())))
        req=await ws.requests.get()
        self.assertEqual(req['method'],'connect')
        self.assertEqual(req['params']['scopes'],['operator.read'])
        await ws.respond(req,{})
        req=await ws.requests.get()
        self.assertEqual(req['method'],'sessions.subscribe')
        await ws.respond(req,{},False)
        with self.assertRaises(RuntimeError):
            await task
        self.assertTrue(ws.requests.empty())
        self.assertFalse(feed.snapshot(clock_ms())['connected'])

    async def test_offline_sampler_at_start_and_immediately_after_socket_loss(self):
        calls = asyncio.Queue()
        work = Mock()
        async def refresh(call, now):
            await calls.put(call)
        work.refresh = refresh
        sampler = BackgroundSampler(work)
        polling = asyncio.create_task(sampler.run())
        ws = Socket()
        feed = ActivityFeed()
        try:
            self.assertIsNone(await asyncio.wait_for(calls.get(), 1))
            await ws.incoming.put({'type':'event', 'event':'connect.challenge'})
            watching = asyncio.create_task(watch_connection(ws, 'fixture', feed, sampler))
            for method in ['connect', 'sessions.subscribe']:
                req = await ws.requests.get()
                self.assertEqual(req['method'], method)
                await ws.respond(req, {})
            self.assertTrue(callable(await asyncio.wait_for(calls.get(), 1)))
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
            self.assertIsNone(await asyncio.wait_for(calls.get(), 1))
            work.invalidate.assert_called_once()
        finally:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)

    async def test_interleaved_replies_use_wire_payload(self):
        ws=Socket()
        feed=ActivityFeed()
        client=GatewayReader(ws,feed)
        listener=asyncio.create_task(client.listen())
        try:
            a=asyncio.create_task(client.call('sessions.list',{}))
            b=asyncio.create_task(client.call('sessions.processes.list',{'key':PARENT}))
            ra,rb=await ws.requests.get(),await ws.requests.get()
            await ws.respond(rb,{'processes':[]})
            await ws.incoming.put(event(PEER,True))
            await ws.respond(ra,page([]))
            self.assertEqual(await a,page([]))
            self.assertEqual(await b,{'processes':[]})
            self.assertIn('duijiaoxia', feed.snapshot(clock_ms())['agents'])
        finally:
            listener.cancel()
            await asyncio.gather(listener,return_exceptions=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
