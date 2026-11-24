"""Read-only background work for the employee cards. No calls to model or media providers."""
import asyncio
import datetime
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

try:
    from compression import zstd
except ImportError:
    import zstandard as zstd


FRESH_MS = 45_000
REMOTE_FRESH_MS = 300_000
ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}"
KLING_ID = re.compile(r"^(Task submitted / 任务已提交|Task ID / 任务 ID): (" + ID + r")\s*$", re.M)
KLING_STATUS = re.compile(r"^Status / 状态: ([a-z_]+)\s*$", re.M)
FINISHED = {"completed", "failed", "killed", "cancelled", "canceled", "succeed", "succeeded", "timed_out"}
ACTIVE = {"running", "processing", "in_progress"}
QUEUED = {"queued", "pending", "accepted", "submitted"}
MEDIA_TOOLS = {"video_generate", "image_generate", "music_generate"}
DECODE_ERRORS = (ValueError, TypeError, UnicodeError, RecursionError, zstd.ZstdError)


def read_only(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)


def raw_event(text, blob):
    return text.encode("utf-8") if text is not None else zstd.decompress(blob)


def session_owner(key):
    parts = key.split(":", 2) if isinstance(key, str) else []
    return parts[1] if len(parts) == 3 and parts[0] == "agent" and parts[2] else None


def process_births(pids):
    """One ps per batch. None means unable to inspect, a missing PID means it exited."""
    results = {}
    ordered = sorted(set(pids))
    for pos in range(0, len(ordered), 128):
        batch = ordered[pos:pos + 128]
        try:
            output = subprocess.run(
                ["/bin/ps", "-p", ",".join(map(str, batch)), "-o", "pid=,state=,lstart="],
                env={**os.environ, "LC_ALL": "C"}, capture_output=True, text=True, timeout=2,
            )
            if output.returncode not in (0, 1) or output.stderr.strip():
                return None
            for line in output.stdout.splitlines():
                pid, state, started = line.strip().split(None, 2)
                if "Z" not in state:
                    date = datetime.datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
                    results[int(pid)] = int(date.timestamp())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    return results


@dataclass
class LocalJob:
    owner: str
    key: str
    window: str
    process_id: str
    started: int
    pid: int
    confirmed: int
    instance: str = ""
    verified: bool = False
    reused: bool = False
    launch_seq: int = 0
    call_id: str = ""
    cwd: str = ""
    remote_id: str = ""


@dataclass(frozen=True)
class Receipt:
    key: str
    timestamp: int
    state: str


class BackgroundWork:
    """Refresh in one async worker; HTTP readers only access a replaced snapshot."""

    def __init__(self, agents_dir, *, births=process_births, scan_seconds=5, session_limit=16):
        self.root = Path(agents_dir)
        self.script = self.root.parent / "skills/klingai/scripts/kling.mjs"
        self.births = births
        self.scan_seconds = scan_seconds
        self.session_limit = session_limit
        self.cursors = {}
        self.local = {}
        self.remote_processes = {}
        self.remote = {}
        self.native = {}
        self.terminal_remote = set()
        self.owners = set()
        self.good_at = {}
        self.bad_rows = {}
        self.last_rpc = {}
        self.lock = asyncio.Lock()
        self._published = (False, True, 0, ())

    def _drop_owner(self, owner):
        self.terminal_remote = {i for i in self.terminal_remote if i[0] != owner}
        self.bad_rows.pop(owner, None)
        for jobs in (self.local, self.remote_processes, self.remote, self.native):
            for identity in [k for k in jobs if k[0] == owner]:
                jobs.pop(identity)

    def _kling_call(self, db, window, seq, call_id, cwd):
        # An exec result alone could be cat/echo/reading an old provider response.
        if not isinstance(call_id, str) or not call_id:
            return None
        rows = db.execute("""
            SELECT event_json, event_zstd FROM transcript_events
            WHERE session_id=? AND seq<? AND message_role='assistant' ORDER BY seq DESC LIMIT 16
        """, (window, seq))
        for text, blob in rows:
            raw = raw_event(text, blob)
            if b"kling.mjs" not in raw:
                continue
            blocks = json.loads(raw).get("message", {}).get("content", [])
            for call in blocks if isinstance(blocks, list) else []:
                if not isinstance(call, dict) or (call.get("id"), call.get("name"), call.get("type")) != (call_id, "exec", "toolCall"):
                    continue
                args = call.get("arguments")
                cmd = args.get("command") if isinstance(args, dict) else None
                if not isinstance(cmd, str) or any(c in cmd for c in "\n`$|;&<>"):
                    return None
                try:
                    argv = shlex.split(cmd)
                except ValueError:
                    return None
                if len(argv) < 4 or argv[0] not in ("node", "/opt/homebrew/bin/node", "/usr/local/bin/node") or argv[2] not in ("video", "image"):
                    return None
                path = Path(argv[1]).expanduser()
                wd = args.get("workdir") or cwd
                if not path.is_absolute():
                    if not isinstance(wd, str) or not os.path.isabs(wd):
                        return None
                    path = Path(wd) / path
                if path.resolve() != self.script.resolve():
                    return None
                if "--task_id" in argv:
                    pos = argv.index("--task_id") + 1
                    return ("query", argv[pos]) if pos < len(argv) else None
                return "submit", None
        return None

    def _process_origin(self, db, owner, key, window, seq, process_id):
        # A native exit may reach the dashboard before its tool transcript. Only
        # recover this command when there is a real provider receipt to match.
        if not isinstance(process_id, str):
            return None
        rows = db.execute("""
            SELECT seq, event_json, event_zstd FROM transcript_events
            WHERE session_id=? AND seq<? AND message_role='toolResult'
            AND (event_json IS NOT NULL OR navigation_json IS NULL
                 OR json_extract(navigation_json,'$.model.message.toolName')='exec')
            ORDER BY seq DESC
        """, (window, seq))
        for launch_seq, text, blob in rows:
            raw = raw_event(text, blob)
            if process_id.encode() not in raw:
                continue
            msg = json.loads(raw).get("message") or {}
            detail = msg.get("details")
            if not isinstance(detail, dict) or msg.get("toolName") != "exec":
                continue
            if (detail.get("sessionId"), detail.get("status")) == (process_id, "running"):
                return LocalJob(owner, key, window, process_id, detail.get("startedAt", 0), 0, 0,
                                launch_seq=launch_seq, call_id=msg.get("toolCallId"), cwd=detail.get("cwd"))
        return None

    def _native_media(self, owner, key, window, name, details, stamp):
        prefix = (owner, window, name)
        if details.get("action") == "status" and details.get("active") is False:
            # This tool's real, current status query covers all its jobs in this exact window.
            for ident in [i for i, old in self.native.items() if i[:3] == prefix and old.timestamp <= stamp]:
                self.native.pop(ident)
                self.terminal_remote.add(ident)
            return
        entries = details.get("tasks")
        for item in entries if isinstance(entries, list) else [details]:
            task = item.get("task") if isinstance(item, dict) else None
            task_id = task.get("taskId") if isinstance(task, dict) else None
            if not isinstance(task_id, str) or not re.fullmatch("tool:" + name + r":[0-9a-f-]{36}", task_id):
                continue
            if task.get("runId", task_id) != task_id:
                continue
            ident = (*prefix, task_id)
            if ident in self.terminal_remote:
                continue
            old = self.native.get(ident)
            if old and stamp < old.timestamp:
                continue
            status = item.get("status")
            if status in FINISHED or (item.get("count") and item.get("attachments")):
                self.native.pop(ident, None)
                self.terminal_remote.add(ident)
            elif item.get("async") is True:
                noun = name.split("_")[0]
                # OpenClaw sets status=running even at admission. Its producer
                # distinguishes 'Queued ...' from 'Generating ...' in progressSummary.
                progress = item.get("progressSummary")
                if status in ACTIVE and progress in (f"Generating {noun}", f"Saving generated {noun}"):
                    state = "processing"
                elif status in QUEUED or status == "started" or progress == f"Queued {noun} generation":
                    state = "submitted"
                else:
                    state = "unknown"
                self.native[ident] = Receipt(key, stamp, state)

    def _record(self, db, owner, key, window, seq, stamp, message):
        if session_owner(key) != owner:
            return
        role, name = message.get("role"), message.get("toolName")
        if role == "user":
            # Completion is a host-authored turn with provenance fields *outside* text.
            provenance = message.get("provenance") or {}
            name = provenance.get("sourceTool") if isinstance(provenance, dict) else None
            if name in MEDIA_TOOLS and (provenance.get("kind"), provenance.get("sourceChannel")) == ("inter_session", "internal"):
                source = provenance.get("sourceSessionKey")
                if isinstance(source, str) and source.startswith(name + ":"):
                    ident = (owner, window, name, source[len(name)+1:])
                    if ident in self.native:
                        self.native.pop(ident)
                        self.terminal_remote.add(ident)
            return
        details = message.get("details")
        if role != "toolResult" or not isinstance(details, dict):
            return
        if name in MEDIA_TOOLS:
            self._native_media(owner, key, window, name, details, stamp)
            return
        if name not in ("exec", "process"):
            return
        old = None
        status, process_id = details.get("status"), details.get("sessionId")
        if isinstance(process_id, str) and re.fullmatch(ID, process_id):
            ident = (owner, window, process_id)
            old = self.local.get(ident)
            started = details.get("startedAt")
            if name == "exec" and status == "running" and type(started) is int and started > 0:
                cwd, pid = details.get("cwd"), details.get("pid")
                # Restrict OS fallback to a local OpenClaw working directory.
                local_cwd = isinstance(cwd, str) and Path(cwd).is_relative_to(self.root.parent)
                pid = pid if local_cwd and type(pid) is int and pid > 0 else 0
                if old is None or started > old.started:
                    linked = self.remote_processes.get(ident)
                    if linked and linked.started != started:
                        self.remote_processes.pop(ident)
                    self.local[ident] = LocalJob(owner, key, window, process_id, started, pid, stamp, reused=bool(old),
                                                 launch_seq=seq, call_id=message.get("toolCallId"), cwd=cwd if local_cwd else "")
            if (name == "process" and status in FINISHED and old and stamp >= old.started
                    and (started == old.started or (started is None and not old.reused))):
                self.local.pop(ident)
        output = details.get("aggregated") or details.get("tail")
        if not isinstance(output, str) or "Status / 状态:" not in output:
            return
        ids, states = KLING_ID.findall(output), KLING_STATUS.findall(output)
        if not states or len({i for _, i in ids}) > 1:
            return
        process_key = (owner, window, process_id)
        job = old if name == "process" else self.local.get(process_key)
        if name == "process" and not job:
            job = self.remote_processes.get(process_key)
            if not job and ids:
                job = self._process_origin(db, owner, key, window, seq, process_id)
            if not job:
                return
        task_id = ids[0][1] if ids else job.remote_id if job else ""
        if not task_id:
            return
        if job and job.remote_id == task_id:
            source = ("query", task_id)
        elif job:
            source = self._kling_call(db, window, job.launch_seq, job.call_id, job.cwd)
        else:
            source = self._kling_call(db, window, seq, message.get("toolCallId"), details.get("cwd"))
        if not source:
            return
        ident = (owner, task_id)
        if ident in self.terminal_remote:
            return
        previous = self.remote.get(ident)
        if source[0] == "query":
            if source[1] != task_id or not previous:
                return
        elif not any(label.startswith("Task submitted") for label, _ in ids):
            return
        if previous and stamp < previous.timestamp:
            return
        state = states[-1]
        if state in FINISHED:
            self.terminal_remote.add(ident)
            self.remote.pop(ident, None)
            for proc, linked in list(self.remote_processes.items()):
                if linked.owner == owner and linked.remote_id == task_id:
                    self.remote_processes.pop(proc)
        elif state in ACTIVE | QUEUED:
            self.remote[ident] = Receipt(previous.key if previous else key, stamp, state)
            if job:
                job.remote_id = task_id
                self.remote_processes[process_key] = job

    def _ingest(self, db, owner, row):
        row_id, window, key, seq, stamp, text, blob = row
        try:
            raw = raw_event(text, blob)
            if (b'"sessionId"' in raw or b'Status /' in raw or b'_generate"' in raw):
                msg = json.loads(raw).get("message")
                if isinstance(msg, dict):
                    self._record(db, owner, key, window, seq, stamp, msg)
        except DECODE_ERRORS:
            self.bad_rows.setdefault(owner, set()).add(row_id)
            return
        self.bad_rows.get(owner, set()).discard(row_id)

    def _scan_db(self, path, owner, deadline):
        st = path.stat()
        fingerprint = (st.st_dev, st.st_ino)
        with closing(read_only(path)) as db:
            # Hold one SQLite read snapshot through this slice, not across checks.
            db.execute("BEGIN")
            cur = self.cursors.get(owner)
            if cur and cur[0] == fingerprint:
                anchor = db.execute("SELECT session_id,seq,created_at FROM transcript_events WHERE rowid=?", (cur[1],)).fetchone()
                if anchor != cur[2]:
                    cur = None
            if not cur or cur[0] != fingerprint:
                self._drop_owner(owner)
                cur = (fingerprint, 0, None)
            last = cur[1]
            end = db.execute("SELECT max(rowid) FROM transcript_events").fetchone()[0] or 0
            columns = """SELECT e.rowid, e.session_id, w.session_key, e.seq, e.created_at,
                             e.event_json, e.event_zstd FROM transcript_events e
                         JOIN session_windows w ON w.session_id=e.session_id"""
            for row_id in sorted(self.bad_rows.get(owner, ()))[:32]:
                row = db.execute(columns + " WHERE e.rowid=?", (row_id,)).fetchone()
                if row:
                    self._ingest(db, owner, row)
                else:
                    self.bad_rows[owner].discard(row_id)
            while last < end:
                stop = min(end, last + 1600)
                rows = db.execute(columns + """
                    WHERE e.rowid>? AND e.rowid<=? AND e.message_role IN ('toolResult','user')
                    AND (e.event_json IS NOT NULL OR e.navigation_json IS NULL OR e.message_role='user'
                         OR CASE WHEN json_valid(e.navigation_json)
                            THEN json_extract(e.navigation_json,'$.model.message.toolName') IN ('exec','process','video_generate','image_generate','music_generate')
                            ELSE 1 END)
                    ORDER BY e.rowid
                """, (last, stop))
                for row in rows:
                    self._ingest(db, owner, row)
                last = stop
                if time.monotonic() >= deadline:
                    break
            # Sparse rowids following deletions need a real anchor.
            anchor = db.execute("SELECT rowid,session_id,seq,created_at FROM transcript_events WHERE rowid<=? ORDER BY rowid DESC LIMIT 1", (last,)).fetchone()
            if anchor:
                self.cursors[owner] = (fingerprint, anchor[0], anchor[1:])
            return last == end and not self.bad_rows.get(owner)

    def _scan(self):
        files = sorted(self.root.glob("*/agent/openclaw-agent.sqlite"))
        found = {path.parent.parent.name for path in files}
        self.owners.update(found)
        good = set()
        deadline = time.monotonic() + self.scan_seconds
        for path in files:
            owner = path.parent.parent.name
            try:
                if self._scan_db(path, owner, deadline):
                    good.add(owner)
            except (sqlite3.Error, OSError):
                continue
        return good, self.owners - good

    async def _list(self, call, owner, key, semaphore):
        async with semaphore:
            try:
                return await asyncio.wait_for(call("sessions.processes.list", {"key": key, "agentId": owner}), 2.0)
            except Exception:
                return None

    async def refresh(self, call, now_ms):
        async with self.lock:
            scan = asyncio.create_task(asyncio.to_thread(self._scan))
            try:
                good, incomplete = await asyncio.shield(scan)
            except asyncio.CancelledError:
                # Keep the writer lock until the SQLite thread leaves private state.
                await scan
                raise
            births = await asyncio.to_thread(self.births, {j.pid for j in self.local.values() if j.pid})
            pending = {}
            for job in self.local.values():
                pending.setdefault((job.owner, job.key), []).append(job)
            self.last_rpc = {key: value for key, value in self.last_rpc.items() if key in pending}
            keys = sorted(pending, key=lambda k: (self.last_rpc.get(k, 0), -max(j.started for j in pending[k])))[:self.session_limit] if call else []
            semaphore = asyncio.Semaphore(4)
            replies = await asyncio.gather(*(self._list(call, owner, key, semaphore) for owner, key in keys))
            results = dict(zip(keys, replies))
            for identity in keys:
                self.last_rpc[identity] = now_ms
            for identity, jobs in pending.items():
                response = results.get(identity)
                for job in jobs:
                    ident = (job.owner, job.window, job.process_id)
                    same_window = isinstance(response, dict) and response.get("sessionId") == job.window
                    rows = response.get("processes") if same_window else None
                    row = next((r for r in rows if isinstance(r, dict) and r.get("processId") == job.process_id), None) if isinstance(rows, list) else None
                    if row:
                        same_start = row.get("startedAt") == job.started and isinstance(row.get("instanceId"), str) and row["instanceId"]
                        if not same_start or (job.instance and row["instanceId"] != job.instance) or row.get("status") in FINISHED:
                            self.local.pop(ident)
                            continue
                        if row.get("status") == "running":
                            job.instance, job.confirmed, job.verified = row["instanceId"], now_ms, True
                            continue
                    birth = births.get(job.pid) if births is not None and job.pid else None
                    if births is not None and job.pid and (birth is None or birth > job.started // 1000):
                        self.local.pop(ident)
                    elif births is not None and job.pid and birth == job.started // 1000:
                        job.confirmed, job.verified = now_ms, True
                    elif isinstance(rows, list) and not row and response.get("truncated") is False:
                        self.local.pop(ident)
                    elif identity in results or not call:
                        job.verified = False
            records = [(j.owner, j.key, "working" if j.verified else "unknown", "background" if j.verified else "uncertain", j.confirmed, FRESH_MS)
                       for j in self.local.values()]
            for owner in good:
                self.good_at[owner] = now_ms
            for owner in incomplete:
                records.append((owner, "", "unknown", "uncertain", self.good_at.get(owner, 0), FRESH_MS))
            for entries in (self.native, self.remote):
                for identity, r in entries.items():
                    owner = identity[0]
                    state, kind = ("working", "remote") if r.state in ACTIVE else ("waiting", "submitted")
                    if owner in incomplete or r.state == "unknown":
                        state, kind = "unknown", "uncertain"
                    records.append((owner, r.key, state, kind, r.timestamp, REMOTE_FRESH_MS))
            ready = bool(good)
            self._published = (ready, not ready, now_ms if ready else self._published[2], tuple(records))

    def invalidate(self):
        ready, _, stamp, records = self._published
        lost = tuple((owner, key, "unknown", "uncertain", confirmed, ttl) if kind == "background"
                     else (owner, key, state, kind, confirmed, ttl) for owner, key, state, kind, confirmed, ttl in records)
        self._published = (ready, True, stamp, lost)

    @staticmethod
    def _availability(published, now_ms):
        ready, stale, stamp, _ = published
        return ready, stale or not stamp or not (0 <= now_ms - stamp <= FRESH_MS), stamp

    def availability(self, now_ms):
        return self._availability(self._published, now_ms)

    def read(self, now_ms):
        published = self._published
        return self._snapshot(published[3], now_ms), self._availability(published, now_ms)

    def snapshot(self, now_ms):
        return self._snapshot(self._published[3], now_ms)

    @staticmethod
    def _snapshot(records, now_ms):
        agents, children = {}, {}
        priority = {"working": 3, "waiting": 2, "unknown": 1}
        for owner, key, state, kind, confirmed, max_age in records:
            if not 0 <= now_ms - confirmed <= max_age:
                state, kind = "unknown", "uncertain"
            child = key.startswith(f"agent:{owner}:subagent:")
            if child and state == "working":
                children.setdefault(owner, set()).add(key)
            old = agents.get(owner)
            if not old or (priority[state], confirmed) > (priority[old["state"]], old["confirmedAt"]):
                agents[owner] = {"state": state, "kind": kind, "confirmedAt": confirmed, "viaSubagent": child}
        for owner, record in agents.items():
            active = sorted(children.get(owner, ()))
            record.update(activeChildKeys=active, activeChildren=len(active))
        return agents
