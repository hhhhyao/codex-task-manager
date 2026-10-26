"""Transient task views. Each backend remains the authority for its records."""

import datetime as dt


SOURCE_FILTERS = ("all", "ctm", "repowire")
SOURCES = (("tasks", "CTM"), ("schedules", "RW schedules"), ("recurring_jobs", "RW jobs"))
SCHEDULE_FIELDS = ("schedule_id", "from_peer", "to_peer", "text", "fire_at", "kind", "circle", "cron", "created_at")
JOB_FIELDS = ("calendar_id", "recurring_id", "job_id", "work_id", "title", "kind", "state", "state_reason",
              "owner_peer_id", "assigned_peer_id", "created_by_peer_id", "cron", "next_due_at", "created_at",
              "updated_at", "last_occurrence_work_id", "last_materialized_at", "circle")


def moment(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def when(value):
    parsed = moment(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else "-"


def collection(value, key, fields, id_fields):
    if not isinstance(value, dict) or key not in value:
        raise ValueError("unexpected collection response")
    rows = value[key]
    if rows is None:  # Repowire returns null for some empty collections.
        return []
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("unexpected collection response")
    result = []
    for row in rows:
        if any(row.get(k) is not None and not isinstance(row[k], str) for k in fields):
            raise ValueError("unexpected record field type")
        if not any(isinstance(row.get(k), str) and row[k] for k in id_fields):
            raise ValueError("record has no stable id")
        result.append({k: row[k] for k in fields if k in row})
    return result


def schedules(value):
    return collection(value, "schedules", SCHEDULE_FIELDS, ("schedule_id",))


def recurring_jobs(value):
    return collection(value, "recurring", JOB_FIELDS, ("calendar_id", "recurring_id", "job_id", "work_id"))


def source_errors(snapshot, source="all"):
    return [f"{label}: {snapshot[key]['error']}" for key, label in SOURCES
            if key in snapshot and snapshot[key].get("error") and
            (source == "all" or (source == "ctm") == (key == "tasks"))]


def summary(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    body = [line for line in lines if not (line.startswith("[") and line.endswith("]"))]
    return (body or lines or [""])[0][:160]


def peer_info(identifier, peers):
    if not identifier:
        return "-", "unassigned"
    peer = peers.get(identifier)
    if peer is None:
        return identifier, "unknown"
    name = peer.get("session_name") or peer.get("display_name") or peer.get("name") or identifier
    state = ("offline" if peer.get("status") == "offline" else "no input" if peer.get("addressable") is False else
             "disconnected" if peer.get("ws_connected") is False else
             peer.get("inbound_status") or peer.get("status") or "unknown")
    return name, state


def task_overview(snapshot):
    peers = {p.get("peer_id"): p for p in snapshot.get("peers", {}).get("data") or []}
    rows = []
    for raw in snapshot.get("tasks", {}).get("data") or []:
        row = dict(raw)
        identity = raw["id"]
        row.update(view_id="ctm:"+identity, source="CTM", manager="ctm", source_id=identity)
        deadlines = [(kind, raw.get(field)) for kind, field in
                     (("check", "next_check_at"), ("review", "next_review_at"), ("flush", "next_flush_at"))
                     if not raw.get("source_done") or kind == "flush"]
        deadlines = [(kind, value) for kind, value in deadlines if moment(value)]
        row["next_kind"], row["next_at"] = min(deadlines, key=lambda pair: moment(pair[1])) if deadlines else (None, None)
        owner, target = raw.get("owner") or {}, raw.get("target") or {}
        row["owner_name"] = peer_info(owner.get("peer_id"), peers)[0] if isinstance(owner, dict) else "-"
        row["target_name"], row["target_state"] = peer_info(target.get("peer_id") if isinstance(target, dict) else None, peers)
        row["management"] = {"inspect": ["ctm", "show", identity], "cancel": ["ctm", "cancel", identity]}
        rows.append(row)
    for raw in snapshot.get("schedules", {}).get("data") or []:
        identity = raw["schedule_id"]
        text = summary(raw.get("text")) or "Scheduled message " + identity
        row = {**raw, "view_id": "repowire:schedule:"+identity, "source": "RW schedule", "manager": "repowire",
               "source_id": identity, "name": text, "description": text, "mode": "cron" if raw.get("cron") else "once",
               "status": "scheduled", "next_at": raw.get("fire_at"), "next_kind": "fire",
               "last_sent_at": None, "buffered": None, "next_review_at": None,
               "management": {"inspect": ["repowire", "schedule", "list"],
                              "cancel": ["repowire", "schedule", "delete", identity]}}
        row["owner_name"] = peer_info(raw.get("from_peer"), peers)[0]
        row["target_name"], row["target_state"] = peer_info(raw.get("to_peer"), peers)
        row["delivery_note"] = "Schedule presence/advancement does not establish successful delivery or handling."
        rows.append(row)
    for raw in snapshot.get("recurring_jobs", {}).get("data") or []:
        identity = next(raw[k] for k in ("calendar_id", "recurring_id", "job_id", "work_id") if raw.get(k))
        row = {**raw, "view_id": "repowire:job:"+identity, "source": "RW job", "manager": "repowire",
               "source_id": identity, "name": raw.get("title") or identity, "description": raw.get("title"),
               "mode": "cron", "status": raw.get("state") or "unknown", "next_at": raw.get("next_due_at"),
               "next_kind": "materialize", "buffered": None, "next_review_at": None,
               "management": {"inspect": ["repowire", "jobs", "show", identity],
                              "cancel": ["repowire", "jobs", "cancel", identity]}}
        row["owner_name"] = peer_info(raw.get("owner_peer_id") or raw.get("created_by_peer_id"), peers)[0]
        row["target_name"], row["target_state"] = peer_info(raw.get("assigned_peer_id"), peers)
        row["delivery_note"] = "Last materialization is occurrence creation, not proof of execution or delivery success."
        rows.append(row)
    for row in rows:
        row["source_instance"] = snapshot.get("state_dir" if row["manager"] == "ctm" else "repowire_endpoint")
    return rows


def visible_tasks(snapshot, source="all", include_inactive=False):
    rows = snapshot.get("task_overview")
    if rows is None:
        rows = task_overview(snapshot)
    rows = [r for r in rows if (source == "all" or r["manager"] == source) and
            (include_inactive or r.get("status") not in {"cancelled", "canceled", "finished", "completed", "deleted"})]
    return sorted(rows, key=lambda r: (moment(r.get("next_at")) or dt.datetime.max.replace(tzinfo=dt.timezone.utc), r["view_id"]))
