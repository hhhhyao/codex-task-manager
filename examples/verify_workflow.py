#!/usr/bin/env python3
"""About 50 seconds of isolated fake work. Real delivery is opt-in by task ID."""

import argparse
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_task_manager.client import request
from codex_task_manager.common import socket_path, stamp, state_directory


class Receiver(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.messages.append({"at": time.time(), "body": body})
        text = body["text"]
        if "Task: SLOW\n" in text:
            time.sleep(1.5)
        code = 400 if "Task: REJECT\n" in text else 500 if "Task: UNCERTAIN\n" in text and not self.server.recovered else 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": code == 200, "hook_delivery": {"status": "accepted" if code == 200 else "failed"}}).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--completion-task-id", help="Optional pre-authorized self-notification task in the shared daemon")
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=False)
    state = root / "isolated-state"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.messages, server.recovered = [], False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "CODEX_TASK_MANAGER_REPOWIRE_URL": f"http://127.0.0.1:{server.server_port}",
           "REPOWIRE_DAEMON__AUTH_TOKEN": "fake-verification-token"}
    daemon, producer = None, None
    log = (root / "process.log").open("a")
    tasks, snapshots, results = {}, {}, {}
    started = time.time()
    report = {"started_at": stamp(started), "transport": "isolated local HTTP receiver", "checks": results}

    def rpc(op, **kwargs):
        return request(state, op, **kwargs)

    def wait_ready():
        until = time.monotonic() + 5
        while time.monotonic() < until:
            try:
                return rpc("ping")
            except ValueError:
                if daemon.poll() is not None:
                    raise RuntimeError("isolated daemon exited; inspect process.log")
                time.sleep(.05)
        raise RuntimeError("isolated daemon startup timeout")

    def start_daemon():
        nonlocal daemon
        daemon = subprocess.Popen([sys.executable, "-m", "codex_task_manager", "--state-dir", str(state),
                                  "daemon", "run", "--retention-seconds", "2"], env=env,
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        wait_ready()

    def stop_daemon():
        if daemon is not None and daemon.poll() is None:
            rpc("stop")
            daemon.wait(timeout=20)
            if daemon.returncode:
                raise RuntimeError("isolated daemon failed")

    def submit(name, **values):
        owner = {"peer_id": "fake-owner-b" if name == "BUFFER" else "fake-owner-a",
                 "thread_id": "00000000-0000-4000-8000-000000000002" if name == "BUFFER" else "00000000-0000-4000-8000-000000000001"}
        spec = {"name": name, "owner": owner, "cwd": str(root), "mode": "repeat", "window_seconds": 0,
                "next_action": "Inspect the fake result and continue this verification only.", **values}
        path = root / f"{name.lower()}.task.json"
        path.write_text(json.dumps(spec, indent=2) + "\n")
        command = subprocess.run([sys.executable, "-m", "codex_task_manager", "--state-dir", str(state),
                                  "submit", str(path)], env=env, capture_output=True, text=True, timeout=10)
        if command.returncode:
            raise RuntimeError(command.stderr)
        task = json.loads(command.stdout)
        tasks[name] = task["id"]
        return task

    def emit(name, event_id, text, **values):
        return rpc("emit", id=tasks[name], events=[{"id": event_id, "text": text, **values}])

    def messages(name):
        return [entry for entry in server.messages if f"Task: {name}\n" in entry["body"]["text"]]

    def checkpoint(label):
        snapshots[label] = rpc("list", include_finished=True)
        print(json.dumps({"at": stamp(), "stage": label, "messages": len(server.messages)}), flush=True)

    try:
        start_daemon()
        check = root / "check.py"
        check.write_text("import json\nfrom pathlib import Path\n"
                         "v=json.loads(Path('result.json').read_text())\n"
                         "print(json.dumps({'events':[{'id':'real-completion','text':'Fake computation completed: stdout=5050, returncode=0','immediate':True,'detail_path':str(Path('result.json').resolve())}] if v['state']=='completed' else [],'status':v['state'],'done':v['state']=='completed'}))\n")
        (root / "result.json").write_text(json.dumps({"state": "running"}))
        origin = time.monotonic()
        report["workflow_started_at"] = stamp()
        producer_code = ("import json,os,time;from pathlib import Path;time.sleep(43);value=sum(range(1,101));"
                         "p=Path('result.tmp');p.write_text(json.dumps({'state':'completed','stdout':str(value),'returncode':0}));"
                         "os.replace(p,'result.json');print(value)")
        producer = subprocess.Popen([sys.executable, "-c", producer_code], cwd=root, stdout=log, stderr=log,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        submit("MAIN", mode="once", check=[sys.executable, str(check)], interval_seconds=.5, review_interval_seconds=15)
        submit("BUFFER", window_seconds=6)
        submit("SLOW", window_seconds=.1)
        submit("REJECT", max_attempts=2, retry_seconds=.5)
        submit("UNCERTAIN", retry_seconds=.5)
        submit("DURABLE", window_seconds=25)
        submit("REVIEW_CONTROL", review_interval_seconds=5)
        submit("TIMER_ONCE", mode="once", fire_at=stamp(time.time()+3), message="One-shot timer fired")
        submit("TIMER_REPEAT", interval_seconds=4, message="Repeating timer fired")
        marker = root / "cancelled-check-ran"
        submit("CANCEL_CHECK", check=[sys.executable, "-c", f"import time;from pathlib import Path;time.sleep(30);Path({str(marker)!r}).touch()"], check_timeout=60)
        results["multiple_owners_visible"] = len({v["owner"]["thread_id"] for v in rpc("list")}) == 2
        emit("BUFFER", "A", "Member A completed", at=stamp(time.time()))
        emit("DURABLE", "saved", "Result survives daemon restart")
        emit("REJECT", "rejected", "Expected simulated transport rejection")
        emit("UNCERTAIN", "uncertain", "Expected simulated uncertain delivery")
        emit("SLOW", "first", "First snapshot")
        checkpoint("registered")
        completed = set()
        while time.monotonic()-origin < 49:
            elapsed = time.monotonic()-origin
            for at, name in [(1,"duplicate"),(2,"more"),(3,"cancel"),(4,"important"),(5,"quiet"),
                             (6.5,"disable"),(9,"cancel_timer"),(10,"resolve"),(12,"buffer_check"),
                             (18,"restart"),(20,"enable"),(31,"reviews"),(32,"interval"),(45,"cleanup")]:
                if elapsed < at or name in completed:
                    continue
                completed.add(name)
                if name == "duplicate":
                    results["duplicate_suppressed"] = emit("BUFFER","A","Member A completed")["added"] == 0
                elif name == "more":
                    emit("BUFFER","B","Member B completed",at=stamp(started-1))
                    emit("BUFFER","p10","Progress 10",replace_key="progress")
                    emit("SLOW","second","Second snapshot arrived during send")
                elif name == "cancel":
                    emit("BUFFER","p20","Progress 20",replace_key="progress")
                    snapshots["cancelled_check"] = rpc("cancel",id=tasks["CANCEL_CHECK"])
                elif name == "important":
                    emit("BUFFER","important","Important fact waits for its window",important=True)
                elif name == "quiet":
                    results["important_does_not_force_flush"] = not messages("BUFFER")
                elif name == "disable":
                    rpc("review",id=tasks["REVIEW_CONTROL"],interval_seconds=None)
                elif name == "cancel_timer":
                    if len(messages("TIMER_REPEAT")) < 2:
                        completed.remove(name)
                        continue
                    rpc("cancel",id=tasks["TIMER_REPEAT"])
                    results["repeating_timer_twice_then_cancelled"] = len(messages("TIMER_REPEAT")) == 2
                elif name == "resolve":
                    results["uncertain_not_automatically_retried"] = len(messages("UNCERTAIN")) == 1
                    server.recovered = True
                    rpc("resolve",id=tasks["UNCERTAIN"],outcome="not-delivered")
                elif name == "buffer_check":
                    text = messages("BUFFER")[0]["body"]["text"]
                    results.update(buffer_window_one_message=len(messages("BUFFER"))==1,
                                   progress_merged="Progress 20" in text and "Progress 10" not in text,
                                   important_marked="[IMPORTANT]" in text,
                                   timestamp_sorted=text.index("Member B")<text.index("Member A"),
                                   readable_next_action="Next action:" in text,
                                   inflight_arrivals_preserved=len(messages("SLOW"))==2,
                                   rejection_retries_bounded=len(messages("REJECT"))==2,
                                   other_queues_unblocked=bool(messages("BUFFER")) and bool(messages("TIMER_ONCE")),
                                   review_disable_worked=len(messages("REVIEW_CONTROL"))==1)
                    checkpoint("buffer_and_faults_verified")
                elif name == "restart":
                    before=rpc("show",id=tasks["MAIN"])
                    results["review_keeps_once_observing"] = before["review_count"]==1 and not before["source_done"]
                    stop_daemon()
                    start_daemon()
                    after=rpc("show",id=tasks["MAIN"])
                    results["review_deadline_survives_restart"] = before["next_review_at"]==after["next_review_at"]
                    results["buffer_survives_restart"] = rpc("show",id=tasks["DURABLE"])["buffered"]==1
                    checkpoint("restarted")
                elif name == "enable":
                    rpc("review",id=tasks["REVIEW_CONTROL"],interval_seconds=10)
                elif name == "reviews":
                    if rpc("show",id=tasks["MAIN"])["review_count"] < 2:
                        completed.remove(name)
                        continue
                    rpc("review",id=tasks["REVIEW_CONTROL"],interval_seconds=None)
                    results["review_reenable_worked"] = len(messages("REVIEW_CONTROL"))==2
                    current=rpc("show",id=tasks["MAIN"])
                    results["two_reviews_same_once_task"] = current["review_count"]==2 and not current["source_done"]
                    checkpoint("two_reviews")
                elif name == "interval":
                    if "reviews" not in completed:
                        completed.remove(name)
                        continue
                    rpc("review",id=tasks["MAIN"],interval_seconds=10)
                elif name == "cleanup":
                    for value in rpc("list"):
                        rpc("cancel",id=value["id"])
                    checkpoint("cancelled_remaining")
            if "main_finished" not in snapshots:
                try:
                    current=rpc("show",id=tasks["MAIN"])
                    if current["status"]=="finished":
                        snapshots["main_finished"]=current
                except ValueError:
                    pass
            time.sleep(.1)
        final=snapshots.get("main_finished",{})
        result=json.loads((root/"result.json").read_text())
        results.update(main_completed_after_reviews=final.get("status")=="finished" and final.get("review_count")==3,
                       interval_change_worked=final.get("review_interval_seconds")==10,
                       completion_stops_reviews=len(messages("MAIN"))==4,
                       completed_body_released=final.get("buffered")==0 and final.get("delivery") is None,
                       cancelled_check_did_not_run=not marker.exists(),
                       external_workload_not_cancelled=result.get("stdout")=="5050" and producer.poll()==0,
                       pending_buffer_sent_after_restart=len(messages("DURABLE"))==1,
                       timer_once=len(messages("TIMER_ONCE"))==1,
                       ended_rows_cleaned=rpc("list",include_finished=True)==[])
        report["success"] = all(results.values())
    except Exception as exc:
        report.update(success=False,error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if daemon and daemon.poll() is None:
                for value in rpc("list"):
                    rpc("cancel",id=value["id"])
                stop_daemon()
        except Exception as exc:
            report.update(success=False,cleanup_error=str(exc))
        if producer and producer.poll() is None:
            producer.terminate()
            producer.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join()
        log.close()
        report.update(finished_at=stamp(),duration_seconds=time.time()-started,
                      daemon_stopped=daemon is not None and daemon.poll() is not None,
                      socket_removed=not socket_path(state).exists(),tasks=tasks)
        (root/"messages.json").write_text(json.dumps(server.messages,ensure_ascii=False,indent=2)+"\n")
        (root/"snapshots.json").write_text(json.dumps(snapshots,ensure_ascii=False,indent=2)+"\n")
        (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps({"report":str(root/"report.json"),"success":report.get("success"),"checks":results}),flush=True)
        if args.completion_task_id:
            status="通过" if report.get("success") else "需要诊断"
            event={"id":"fake-workflow-final", "important":True,
                   "text":f"[CTM_FAKE_WORKFLOW_FINAL] 假工作流已结束：{status}。只有本条原始消息实际进入会话才记录 native-receipt.json。读取 report.json 和父目录 goal-handoff.json；按已授权流程恢复原 Goal，然后核对结果、修复必要问题并报告。",
                   "detail_path":str(root/"report.json")}
            accepted=request(state_directory(),"emit",id=args.completion_task_id,events=[event])
            (root/"native-publication.json").write_text(json.dumps(accepted,ensure_ascii=False,indent=2)+"\n")
    return 0 if report.get("success") else 1


if __name__=="__main__":
    raise SystemExit(main())
