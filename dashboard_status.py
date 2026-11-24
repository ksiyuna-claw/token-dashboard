"""Combine current activity evidence for employee cards. No display event starts any work."""
import json
import re
import threading
from pathlib import Path


class ReviewWaits:
    """Read the original audit submission and matching open round, never just its timestamp."""

    def __init__(self, state_root):
        self.root = Path(state_root).resolve()
        self.lock = threading.Lock()
        self.timestamps = {}
        self.last_read = None
        self.waiting = {}

    def _read(self, path):
        try:
            if not path.resolve().is_relative_to(self.root):
                return {}
            with path.open(encoding="utf-8") as stream:
                data = json.load(stream)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def snapshot(self, now_ms):
        with self.lock:
            if self.last_read is not None and 0 <= now_ms - self.last_read < 15000:
                return dict(self.waiting)
            waiting = {}
            stamps = {}
            chain = self.root / "state" / "audit-chain"
            archive = self.root / "workspace/ai_workspace/logs/审核记录"
            for job_path in chain.glob("*.json"):
                if not re.fullmatch(r"[a-f0-9]{64}", job_path.stem):
                    continue
                receipt = job_path.with_suffix(".receipt.json")
                try:
                    stamp = receipt.stat().st_mtime_ns
                except FileNotFoundError:
                    stamp = None
                except OSError:
                    continue
                if stamp is not None and self.timestamps.get(receipt) == stamp:
                    stamps[receipt] = stamp
                    continue
                # The central worker treats a newly registered job with no receipt as pending.
                ack = self._read(receipt) if stamp is not None else {"status": "pending"}
                if ack.get("status") in {"done", "withdrawn", "cancelled", "superseded"}:
                    stamps[receipt] = stamp
                    continue
                if ack.get("status") not in {"pending", "accepted", "reserved", "requesting"}:
                    continue
                job = self._read(job_path)
                flow = job.get("flow")
                project = job.get("projectPath")
                if job.get("phase") != "audit" or not isinstance(flow, str) or not re.fullmatch(r"[\w -]+", flow) or not isinstance(project, str):
                    continue
                if not Path(project).is_absolute():
                    continue
                project = Path(project).resolve()
                if not project.is_relative_to(self.root):
                    continue
                owner, instance, round_no = job.get("owner"), job.get("instance"), job.get("round")
                if not isinstance(owner, str) or not owner or not isinstance(instance, str) or not instance or type(round_no) is not int or round_no <= 0:
                    continue
                slug = flow.replace(" ", "_")
                state_name = f"task_state_{slug}.json"
                record = archive / flow
                state = self._read(project / state_name) or self._read(project / "task_state.json") or self._read(record / state_name)
                rev = state.get("review") or {}
                if not isinstance(rev, dict):
                    continue
                if (state.get("flowId"), state.get("owner"), state.get("taskInstance"), state.get("taskRound"),
                    state.get("status"), rev.get("reviewer"), rev.get("round"), rev.get("status")) != (
                    flow, owner, instance, round_no, "running", "duijiaoxia", round_no, "waiting"):
                    continue
                result_name = f"audit_result_{slug}.json"
                results = (self._read(project / "tmp" / result_name), self._read(record / result_name))
                if any(row.get("round") == round_no and (row.get("task_instance") or row.get("taskInstance")) == instance for row in results):
                    continue
                if owner != "duijiaoxia":
                    waiting[owner] = {"state": "waiting", "kind": "review", "confirmedAt": now_ms}
            self.timestamps, self.waiting, self.last_read = stamps, waiting, now_ms
            return dict(waiting)


def public_activity(agent_id, model_feed, background, availability, reviews, now_ms):
    model_ok = model_feed["ready"] and not model_feed["stale"]
    bg_ready, bg_stale, bg_checked = availability
    bg_ok = bg_ready and not bg_stale
    model = model_feed["agents"].get(agent_id) if model_ok else None
    work = background.get(agent_id)
    candidates = [row for row in (work, model, reviews.get(agent_id)) if row]
    children = set()
    for row in candidates:
        if row["state"] == "working":
            children.update(row.get("activeChildKeys", ()))
    for state in ("working", "waiting", "unknown"):
        selected = next((row for row in candidates if row["state"] == state), None)
        if selected:
            return {"status": state, "activityKind": selected["kind"],
                    "confirmedAt": selected.get("confirmedAt") or None,
                    "activityStale": state == "unknown",
                    "viaSubagent": bool(selected.get("viaSubagent") or selected["kind"] == "subagent"),
                    "subagentActiveCount": len(children)}
    fresh = bool(model_ok and bg_ok)
    timestamps = [ts for ts in (model_feed.get("checkedAt"), bg_checked) if ts]
    return {"status": "idle" if fresh else "unknown", "activityKind": "none" if fresh else "uncertain",
            "confirmedAt": min(timestamps) if len(timestamps) == 2 else None,
            "activityStale": not fresh, "viaSubagent": False, "subagentActiveCount": 0}
