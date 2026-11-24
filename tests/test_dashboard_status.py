import json
import tempfile
import unittest
from pathlib import Path

from dashboard_status import ReviewWaits, public_activity


class StatusTests(unittest.TestCase):
    def test_identity_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "state/audit-chain"
            jobs.mkdir(parents=True)
            project = root / "myproject"
            project.mkdir()
            flow = "文档 工作"
            job = jobs / ("a" * 64 + ".json")
            job.write_text(json.dumps(dict(phase="audit", flow=flow, instance="x1", owner="jiweixia", round=2, projectPath=str(project))))
            state = dict(flowId=flow, taskInstance="x1", taskRound=2, owner="jiweixia", status="running",
                         review=dict(reviewer="duijiaoxia", round=2, status="waiting"))
            statepath = project / "task_state_文档_工作.json"
            statepath.write_text(json.dumps(state))
            reviews = ReviewWaits(root)
            self.assertEqual(list(reviews.snapshot(1000)), ["jiweixia"])
            (project / "tmp").mkdir()
            result = project / "tmp/audit_result_文档_工作.json"
            result.write_text(json.dumps(dict(round=2, task_instance="previous")))
            self.assertIn("jiweixia", reviews.snapshot(18000))
            result.write_text(json.dumps(dict(round=2, task_instance="x1")))
            self.assertEqual(reviews.snapshot(35000), {})
            result.unlink()
            statepath.write_text(json.dumps(dict(state, taskRound=1)))
            self.assertEqual(reviews.snapshot(51000), {})
            statepath.write_text(json.dumps(state))
            receipt = job.with_suffix(".receipt.json")
            receipt.write_text('{"status":"done"}')
            self.assertEqual(reviews.snapshot(67000), {})
            self.assertEqual(reviews.snapshot(83000), {})
            receipt.write_text('{"status":"accepted"}')
            self.assertIn("jiweixia", reviews.snapshot(99000))
            receipt.write_text('{"status":"blocked"}')
            self.assertEqual(reviews.snapshot(115000), {})

    def test_concurrent_progress_is_counted_once_per_child(self):
        m = dict(ready=True, stale=False, checkedAt=42, agents={
            "jiweixia": dict(kind="subagent", state="working", activeChildKeys=["one"], confirmedAt=42)})
        bg = {"jiweixia": dict(kind="background", state="working", viaSubagent=True,
                              activeChildKeys=["one", "two"], confirmedAt=47)}
        review = {"jiweixia": dict(kind="review", state="waiting", confirmedAt=50)}
        self.assertEqual(public_activity("jiweixia", m, bg, (True, False, 47), review, 50)["subagentActiveCount"], 2)
        self.assertEqual(public_activity("jiweixia", m, bg, (True, False, 47), review, 50)["status"], "working")
        self.assertEqual(public_activity("duijiaoxia", m, bg, (True, False, 47), review, 50)["status"], "idle")
        m["stale"] = True
        self.assertEqual(public_activity("jiweixia", m, {}, (True, False, 47), review, 50)["activityKind"], "review")
        self.assertEqual(public_activity("duijiaoxia", m, {}, (True, False, 47), review, 50)["status"], "unknown")

class SessionCacheTests(unittest.TestCase):
    def test_live_status_does_not_queue_behind_history_refresh(self):
        from threading import Event, Thread
        from unittest.mock import patch
        import token_dashboard_server as server

        started, complete = Event(), Event()
        calls = []

        def read():
            calls.append(1)
            started.set()
            complete.wait(2)
            with server._sessions_lock:
                server._sessions_cache.update(data={"sessions": [{"key": "cached"}]}, ts=server.time.time())
            return server._sessions_cache["data"]

        with patch.object(server, "_sessions_cache", {"data": None, "ts": 0}), patch.object(server, "_refresh_sessions", read):
            self.assertTrue(server._get_cached_sessions(nonblocking=True)["_stale"])
            self.assertTrue(started.wait(1))
            for _ in range(5):
                self.assertTrue(server._get_cached_sessions(nonblocking=True)["_stale"])
            result = []
            waiter = Thread(target=lambda: result.append(server._get_cached_sessions()))
            waiter.start()
            complete.set()
            waiter.join(2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result, [{"sessions": [{"key": "cached"}]}])


if __name__ == "__main__":
    unittest.main()
