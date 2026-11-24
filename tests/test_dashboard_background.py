import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard_background import BackgroundWork, FRESH_MS, REMOTE_FRESH_MS, process_births, zstd


@contextmanager
def writable(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        yield connection


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.root = self.state / "agents"
        self.now = int(time.time() * 1000)
        self.key = "agent:writer:subagent:isolated-child"
        self.window = "original-window"
        self.path = self.make_db("writer", self.key)
        self.work = BackgroundWork(self.root, births=lambda _: None)
        self.rpc_rows = []
        self.rpc_key = self.key
        self.rpc_window = self.window
        self.truncated = False
        self.calls = []

    def make_db(self, owner, key):
        path = self.root / owner / "agent/openclaw-agent.sqlite"
        path.parent.mkdir(parents=True)
        with writable(path) as db:
            db.executescript("""
                CREATE TABLE session_windows(session_id TEXT PRIMARY KEY,session_key TEXT);
                CREATE TABLE transcript_events(session_id TEXT,seq INTEGER,created_at INTEGER,
                    event_json TEXT,event_zstd BLOB,navigation_json TEXT,message_role TEXT,PRIMARY KEY(session_id,seq));
            """)
            db.execute("INSERT INTO session_windows VALUES(?,?)", (self.window, key))
        return path

    def event(self, seq, msg, *, compressed=False, offset=0, path=None):
        text = json.dumps({"message": msg}, ensure_ascii=False)
        nav = json.dumps({"model": {"message": {"role": msg["role"], "toolName": msg.get("toolName")}}})
        with writable(path or self.path) as db:
            db.execute("INSERT INTO transcript_events VALUES(?,?,?,?,?,?,?)", (
                self.window, seq, self.now + offset, None if compressed else text,
                zstd.compress(text.encode()) if compressed else None, nav if compressed else None, msg["role"],
            ))

    def result(self, seq, name, details, *, call="c", **kw):
        self.event(seq, {"role":"toolResult","toolCallId":call,"toolName":name,"details":details}, **kw)

    def launch(self, seq=1, *, pid=0, stamp=None, cwd=None, **kw):
        self.result(seq, "exec", {"status":"running","sessionId":"green-shell","pid":pid,
                                  "startedAt":stamp or self.now,"cwd":str(cwd or self.state)}, **kw)

    def registry(self, status, *, started=None, instance="incarnation"):
        return {"processId":"green-shell","startedAt":started or self.now,"instanceId":instance,"status":status,
                "name":"SHOULD NOT LEAK","tail":"SECRET COMMAND STDOUT"}

    async def rpc(self, method, params):
        self.assertEqual(method, "sessions.processes.list")
        self.assertEqual(params, {"key":self.rpc_key,"agentId":"writer"})
        self.calls.append(params)
        return {"sessionId":self.rpc_window,"processes":self.rpc_rows,"truncated":self.truncated}

    async def test_process_rpc_lifecycle_same_agent_and_no_payload_exposed(self):
        self.launch(compressed=True)
        self.rpc_rows = [self.registry("running")]
        self.assertFalse(self.work.availability(self.now)[0])
        await self.work.refresh(self.rpc, self.now)
        self.assertEqual(self.work.availability(self.now), (True,False,self.now))
        row = self.work.snapshot(self.now)["writer"]
        self.assertEqual((row["kind"],row["activeChildKeys"],row["viaSubagent"]), ("background",[self.key],True))
        self.assertNotIn("SECRET", str(row))
        self.assertEqual(self.work.snapshot(self.now + FRESH_MS + 1)["writer"]["state"], "unknown")
        self.rpc_rows = [self.registry("completed")]
        await self.work.refresh(self.rpc, self.now+2)
        self.assertEqual(self.work.snapshot(self.now+2), {})
        await self.work.refresh(self.rpc, self.now+3)
        self.assertEqual(len(self.calls), 2)

    async def test_old_window_and_truncated_list_are_unknown(self):
        self.launch()
        self.rpc_rows = [self.registry("running")]
        self.rpc_window = "other-generation"
        await self.work.refresh(self.rpc, self.now)
        self.assertEqual(self.work.snapshot(self.now)["writer"]["state"], "unknown")
        self.rpc_window, self.rpc_rows, self.truncated = self.window, [], True
        await self.work.refresh(self.rpc, self.now+1)
        self.assertEqual(self.work.snapshot(self.now+1)["writer"]["state"], "unknown")
        self.truncated = False
        await self.work.refresh(self.rpc, self.now+2)
        self.assertEqual(self.work.snapshot(self.now+2), {})

    async def test_changed_instance_never_lights_old_receipt(self):
        self.launch()
        self.rpc_rows = [self.registry("running")]
        await self.work.refresh(self.rpc,self.now)
        self.work.invalidate()
        self.assertEqual(self.work.snapshot(self.now)["writer"]["state"], "unknown")
        self.rpc_rows = [self.registry("running", instance="new-incarnation")]
        await self.work.refresh(self.rpc,self.now+2)
        self.assertEqual(self.work.snapshot(self.now+2), {})

    async def test_compressed_completion_and_incremental_idle_after_restart(self):
        self.launch()
        self.rpc_rows = [self.registry("running")]
        await self.work.refresh(self.rpc,self.now)
        self.result(2,"process",{"status":"completed","sessionId":"green-shell"},compressed=True,offset=12)
        await self.work.refresh(self.rpc,self.now+12)
        self.assertEqual(self.work.snapshot(self.now+12), {})
        self.assertEqual(len(self.calls),1)
        cold = BackgroundWork(self.root,births=lambda _: None)
        await cold.refresh(self.rpc,self.now+13)
        self.assertEqual(cold.snapshot(self.now+13), {})
        self.assertEqual(len(self.calls),1)

    async def test_pid_collision_and_unknown_cwd(self):
        self.work = BackgroundWork(self.root,births=lambda _: {4123:self.now//1000+4})
        self.launch(pid=4123)
        await self.work.refresh(None,self.now+8000)
        self.assertEqual(self.work.snapshot(self.now+8000), {})
        self.launch(seq=2,pid=4123,cwd="/untrusted-other-host")
        await self.work.refresh(None,self.now+8001)
        self.assertEqual(self.work.snapshot(self.now+8001)["writer"]["state"], "unknown")

    async def test_real_local_shell_is_confirmed_during_gateway_loss_then_exits(self):
        # Only an isolated child process. No OpenClaw session, paid job or outbound messages.
        shell = subprocess.Popen(["/bin/sh", "-c", "read line"], stdin=subprocess.PIPE)
        self.addCleanup(lambda: shell.stdin.close() if shell.stdin and not shell.stdin.closed else None)
        try:
            start = process_births([shell.pid])[shell.pid]
            self.work = BackgroundWork(self.root)
            self.launch(pid=shell.pid,stamp=start*1000)
            await self.work.refresh(None,self.now)
            self.assertEqual(self.work.snapshot(self.now)["writer"]["kind"], "background")
            shell.stdin.close()
            shell.wait(timeout=3)
            await self.work.refresh(None,self.now+15_000)
            self.assertEqual(self.work.snapshot(self.now+15_000), {})
        finally:
            if shell.poll() is None:
                shell.kill()
                shell.wait(timeout=2)

    def kling(self, seq, command, output, *, offset=0):
        call = f"call{seq}"
        self.event(seq,{"role":"assistant","content":[{"type":"toolCall","name":"exec","id":call,
                                                       "arguments":{"command":command}}]},offset=offset)
        self.result(seq+1,"exec",{"status":"completed","aggregated":output,"cwd":str(self.state)},
                    call=call,compressed=True,offset=offset)

    async def test_real_kling_output_shape_wait_fresh_expired_finish_and_replay(self):
        cmd = f"node {self.work.script} video"
        self.kling(1,cmd+' --prompt sample --no-wait',"Task submitted / 任务已提交: test-123\nStatus / 状态: submitted")
        await self.work.refresh(None,self.now)
        self.assertEqual(self.work.snapshot(self.now)["writer"]["kind"],"submitted")
        self.kling(3,cmd+' --task_id test-123',"Task ID / 任务 ID: test-123\nStatus / 状态: processing",offset=1000)
        await self.work.refresh(None,self.now+1000)
        self.assertEqual(self.work.snapshot(self.now+1000)["writer"]["kind"],"remote")
        expired = self.now+1001+REMOTE_FRESH_MS
        await self.work.refresh(None,expired)
        stale = self.work.snapshot(expired)["writer"]
        self.assertEqual((stale["state"],stale["confirmedAt"]),("unknown",self.now+1000))
        self.kling(5,cmd+' --task_id test-123',"Task ID / 任务 ID: test-123\nStatus / 状态: succeed",offset=400000)
        self.kling(7,cmd+' --prompt sample --no-wait',"Task submitted / 任务已提交: test-123\nStatus / 状态: submitted")
        await self.work.refresh(None,self.now+400001)
        self.assertEqual(self.work.snapshot(self.now+400001), {})

    async def test_remote_status_from_echo_other_job_and_untrusted_script_is_ignored(self):
        output = "Task submitted / 任务已提交: test-123\nStatus / 状态: processing"
        self.kling(1,"echo pretend",output)
        self.kling(3,"node /tmp/kling.mjs video --no-wait",output)
        self.kling(5,f"node {self.work.script} video --task_id other",output)
        self.kling(7,f"node {self.work.script} video --task_id test-123",output)
        self.kling(9,f"node {self.work.script} video --no-wait ; echo spoof",output)
        await self.work.refresh(None,self.now)
        self.assertEqual(self.work.snapshot(self.now),{})

    async def test_native_media_start_status_and_host_completion(self):
        name = "video_generate"
        job = "tool:video_generate:0e4e165d-c947-4913-9fb9-e87dc78091d6"
        reference = {"taskId":job, "runId":job}
        self.result(1,name,{"async":True,"status":"started","taskId":job,"runId":job,"task":reference},compressed=True)
        await self.work.refresh(None,self.now)
        self.assertEqual(self.work.snapshot(self.now)["writer"]["kind"],"submitted")
        detail = {"action":"status","async":True,"active":True,"existingTask":True,"status":"running",
                  "task":reference,"taskKind":"video_generation","sourceId":"video_generate:sample"}
        self.result(2,name,{**detail,"progressSummary":"Queued video generation"},offset=10)
        await self.work.refresh(None,self.now+10)
        self.assertEqual(self.work.snapshot(self.now+10)["writer"]["kind"],"submitted")
        self.result(3,name,{**detail,"progressSummary":"Generating video"},offset=20)
        await self.work.refresh(None,self.now+20)
        self.assertEqual(self.work.snapshot(self.now+20)["writer"]["kind"],"remote")
        provenance = {"kind":"inter_session","sourceChannel":"internal","sourceTool":name,"sourceSessionKey":name+":"+job}
        self.event(4,{"role":"user","content":json.dumps(provenance)},offset=21)
        self.event(5,{"role":"user","provenance":{**provenance,"sourceChannel":"telegram"}},offset=22)
        await self.work.refresh(None,self.now+22)
        self.assertEqual(self.work.snapshot(self.now+22)["writer"]["kind"],"remote")
        self.event(6,{"role":"user","provenance":provenance},compressed=True,offset=30)
        await self.work.refresh(None,self.now+30)
        self.assertEqual(self.work.snapshot(self.now+30),{})

    async def test_native_same_tool_empty_status_and_completion_do_not_cross_windows(self):
        job = "tool:image_generate:cee4bc86-7209-4c26-a545-d810e66c9900"
        self.result(1,"image_generate",{"async":True,"status":"started","task":{"taskId":job}})
        await self.work.refresh(None,self.now)
        with writable(self.path) as db:
            db.execute("INSERT INTO session_windows VALUES('replacement',?)",(self.key,))
        self.window = "replacement"
        self.result(1,"image_generate",{"action":"status","active":False},offset=1)
        await self.work.refresh(None,self.now+1)
        self.assertEqual(self.work.snapshot(self.now+1)["writer"]["state"],"waiting")
        self.window = "original-window"
        self.result(2,"image_generate",{"action":"status","active":False},offset=2)
        await self.work.refresh(None,self.now+2)
        self.assertEqual(self.work.snapshot(self.now+2),{})

    async def test_long_kling_command_and_compressed_terminal_process_receipt(self):
        cmd = f"node {self.work.script} video --prompt sample"
        self.event(1,{"role":"assistant","content":[{"type":"toolCall","name":"exec","id":"c","arguments":{"command":cmd}}]})
        self.result(2,"exec",{"status":"running","sessionId":"green-shell","startedAt":self.now,
                              "tail":"Task submitted / 任务已提交: kk-11\nStatus / 状态: submitted"})
        await self.work.refresh(None,self.now)
        self.assertEqual(self.work.snapshot(self.now)["writer"]["kind"], "submitted")
        self.result(3,"process",{"status":"running","sessionId":"green-shell","aggregated":"Status / 状态: processing"},offset=2)
        await self.work.refresh(None,self.now+2)
        self.assertEqual(self.work.snapshot(self.now+2)["writer"]["kind"], "remote")
        # Registry cleanup can beat SQLite transcript insertion.
        self.rpc_rows = [self.registry("completed")]
        await self.work.refresh(self.rpc,self.now+2)
        self.assertFalse(self.work.local)
        self.result(4,"process",{"status":"completed","sessionId":"green-shell","aggregated":"Status / 状态: succeed"},compressed=True,offset=3)
        await self.work.refresh(None,self.now+3)
        self.assertEqual(self.work.snapshot(self.now+3),{})

    async def test_partial_role_read_failure_is_localized_and_recoverable(self):
        bad = self.make_db("other", "agent:other:main")
        with writable(bad) as db:
            db.execute("INSERT INTO transcript_events VALUES('original-window',1,?,NULL,?,NULL,'toolResult')",(self.now,b"damaged zstd"))
        self.launch()
        self.rpc_rows = [self.registry("running")]
        await self.work.refresh(self.rpc,self.now)
        result = self.work.snapshot(self.now)
        self.assertEqual((result["writer"]["state"], result["other"]["state"]),("working","unknown"))
        self.assertTrue(self.work.availability(self.now)[0])
        with writable(bad) as db:
            db.execute("DELETE FROM transcript_events WHERE seq=1")
        await self.work.refresh(self.rpc,self.now+20)
        self.assertNotIn("other",self.work.snapshot(self.now+20))


if __name__ == "__main__":
    unittest.main()
