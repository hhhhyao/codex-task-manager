"""Small curses viewer. Refresh work stays off the terminal thread."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
import sys
import time

from .dashboard import VIEWS, agent_visibility_reasons, clean, clip, collect_snapshot, pad, row_id, rows_for, table_layout, text_snapshot


def terminal_palette(curses):
    """Color only foregrounds; never replace the user's terminal background."""
    styles = {"normal": 0, "muted": curses.A_DIM, "accent": curses.A_BOLD,
              "good": 0, "busy": curses.A_BOLD, "warning": curses.A_BOLD, "error": curses.A_BOLD}
    try:
        if not curses.has_colors():
            return styles
        curses.start_color()
        curses.use_default_colors()
        if "NO_COLOR" in os.environ:
            return styles
        for pair, (role, color) in enumerate((("accent", curses.COLOR_CYAN), ("good", curses.COLOR_GREEN),
                                             ("busy", curses.COLOR_CYAN), ("warning", curses.COLOR_YELLOW),
                                             ("error", curses.COLOR_RED)), 1):
            curses.init_pair(pair, color, -1)
            styles[role] |= curses.color_pair(pair)
    except curses.error:
        # Terminals without default-color support stay monochrome.
        return {role: attr & (curses.A_BOLD | curses.A_DIM) for role, attr in styles.items()}
    return styles


def status_role(value):
    value = str(value or "").lower()
    if any(word in value for word in ("error", "failed", "unavailable", "rejected", "ambiguous")):
        return "error"
    if value in {"paused", "buffering", "unknown", "not observed", "delivery_ready", "unmatched", "no input"}:
        return "warning"
    if value in {"checking", "working", "running", "busy", "delivery_sending"}:
        return "busy"
    if value in {"reachable", "online", "connected", "completed", "done", "ok"}:
        return "good"
    return "muted" if value in {"offline", "cancelled", "canceled", "stopped", "subagent", "internal", "archived", "-"} else "normal"


@dataclass
class Selection:
    index: int = 0
    key: str | None = None

    def sync(self, rows):
        keys = [row_id(row) for row in rows]
        self.index = keys.index(self.key) if self.key in keys else min(self.index, max(0, len(rows)-1))
        self.key = keys[self.index] if keys else None

    def move(self, amount, rows):
        self.index = max(0, min(len(rows)-1, self.index + amount))
        self.key = row_id(rows[self.index]) if rows else None


def wrapped_detail(row, width):
    lines = []
    for original in json.dumps(row, ensure_ascii=False, indent=2).splitlines():
        line = clean(original).replace("\t", " ")
        if not line:
            lines.append("")
        while line:
            part = clip(line, max(1, width))
            lines.append(part)
            line = line[len(part):]
    return lines


def interactive(screen, directory, refresh, collector=collect_snapshot):
    import curses
    styles = terminal_palette(curses)
    screen.bkgd(" ", curses.A_NORMAL)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(100)
    selected = [Selection() for _ in VIEWS]
    tab, query, editing, detail, offset = 0, "", False, False, 0
    all_agents, paused = False, False
    snapshot, error = None, None
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(collector, directory)
    next_refresh = time.monotonic() + refresh

    def draw(y, text, attr=0, x=0):
        height, width = screen.getmaxyx()
        if 0 <= y < height:
            try:
                screen.addstr(y, x, clip(text, max(0, width-x-1)), attr)
            except curses.error:
                pass

    try:
        while True:
            now = time.monotonic()
            if future is not None and future.done():
                try:
                    snapshot = future.result()
                    error = None
                except Exception as exc:
                    error = type(exc).__name__ + ": refresh failed; previous snapshot retained"
                future = None
                next_refresh = now + refresh
            if future is None and not paused and now >= next_refresh:
                future = pool.submit(collector, directory)
            height, width = screen.getmaxyx()
            view = VIEWS[tab]
            rows = rows_for(snapshot, view, query, all_agents) if snapshot else []
            selection = selected[tab]
            selection.sync(rows)
            screen.erase()
            stamp = snapshot["updated_at"][:19].replace("T", " ") if snapshot else "loading"
            state = "PAUSED" if paused else "refreshing" if future else f"every {refresh:g}s"
            draw(0, f"Codex Task Manager | read-only | {stamp} UTC | {state}", styles["muted"])
            x = 0
            for i, label in enumerate(VIEWS):
                title = f" {i+1} {label} "
                draw(1, title, styles["accent"] | curses.A_BOLD if i == tab else styles["muted"], x)
                x += len(title)+2
            if height < 10 or width < 55:
                draw(3, "Terminal too small; enlarge to at least 55 x 10. q: quit")
            elif snapshot:
                tasks = snapshot["tasks"]["data"]
                peers = snapshot["peers"]["data"]
                task_count = str(len(tasks)) if tasks is not None else "unavailable"
                peer_count = f"{len(rows_for(snapshot, 'Agents'))}/{len(peers)}" if peers is not None else "unavailable"
                ping = snapshot["ctm"]["data"] or {}
                activity = f"checks {ping.get('checks_running','?')}/sends {ping.get('sends_running','?')}"
                draw(2, f"Tasks {task_count} ({activity}) | Main/all agents {peer_count} | Filter: {query or '-'}", styles["muted"])
                source = snapshot.get("tasks" if view == "Tasks" else "peers", {})
                source_error = source.get("error") if view != "Services" else None
                hint = (" | all records" if all_agents else f" | main sessions; {len(peers)-len(rows_for(snapshot, 'Agents'))} hidden") if view == "Agents" and peers is not None else ""
                draw(3, error or ("Unavailable: " + source_error if source_error else f"{view} | {len(rows)} entries{hint} | Enter: details"), styles["error"] if error or source_error else styles["normal"])
                room = height-(8 if view == "Tasks" and not detail else 7)
                if detail and rows:
                    lines = wrapped_detail(rows[selection.index], width-1)
                    offset = max(0, min(offset, max(0, len(lines)-room)))
                    for i, line in enumerate(lines[offset:offset+room]):
                        draw(4+i, line)
                elif rows:
                    headers, sizes, cells = table_layout(snapshot, view, rows, width-3)
                    draw(4, "  ".join(pad(value, size) for value, size in zip(headers, sizes)), styles["muted"] | curses.A_BOLD, 2)
                    start = max(0, selection.index-(room-2))
                    for i, values in enumerate(cells[start:start+room-1]):
                        active = start+i == selection.index
                        draw(5+i, "›" if active else " ", styles["accent"])
                        x = 2
                        for header, value, size in zip(headers, values, sizes):
                            role = status_role(value) if header in {"STATE", "TURN", "INBOUND"} else "warning" if header == "BUF" and value else "normal"
                            draw(5+i, pad(value, size), styles[role] | (curses.A_BOLD if active else 0), x)
                            x += size+2
                elif not source_error:
                    draw(5, "No matching entries.")
                else:
                    draw(5, "Other tabs remain available; r retries without starting services.")
            else:
                draw(3, error or "Loading service and task status...")
            if rows and not detail and height >= 10 and width >= 55:
                row = rows[selection.index]
                if view == "Tasks":
                    draw(height-4, "Description: " + str(row.get("description") or "-"), styles["muted"])
                context = ("Next action: " + str(row.get("next_action") or "-")) if view == "Tasks" else (
                    "View: " + (", ".join(agent_visibility_reasons(row)) or "main session") + " | Agent: " + str(row.get("display_name") or row.get("name") or row_id(row)) + " | Session: " + str(row.get("session_name") or "-")) if view == "Agents" else "Evidence: " + str(row.get("evidence", "-"))
                draw(height-3, context, styles["muted"])
            draw(height-2, "Filter: " + query + "_  Enter: apply  Esc: finish" if editing else
                 "←/→: page  ↑/↓: select  Enter/Esc: details  /: filter", styles["muted"])
            draw(height-1, "r: refresh  p: pause  a: all/main agents  q: quit | UTC", styles["muted"])
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if editing:
                if key in ("\n", "\r", "\x1b"):
                    editing = False
                elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                    query = query[:-1]
                elif isinstance(key, str) and key.isprintable() and len(query) < 160:
                    query += key
                detail, offset = False, 0
                continue
            if key in ("q", "Q", "\x03"):
                return
            if key in (curses.KEY_LEFT, curses.KEY_RIGHT, "h", "l", "\t", curses.KEY_BTAB, "1", "2", "3"):
                tab = int(key)-1 if key in ("1", "2", "3") else (tab + (-1 if key in (curses.KEY_LEFT, "h", curses.KEY_BTAB) else 1)) % len(VIEWS)
                query, detail, offset = "", False, 0
            elif key in ("j", curses.KEY_DOWN, "k", curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE):
                delta = (-1 if key in ("k", curses.KEY_UP, curses.KEY_PPAGE) else 1) * (max(1,height-7) if key in (curses.KEY_NPAGE,curses.KEY_PPAGE) else 1)
                if detail:
                    offset = max(0, offset+delta)
                else:
                    selection.move(delta, rows)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                detail, offset = not detail, 0
            elif key == "\x1b":
                detail = False
            elif key == "/":
                editing = True
            elif key == "a" and view == "Agents":
                all_agents = not all_agents
            elif key == "p":
                paused = not paused
            elif key == "r" and future is None:
                future = pool.submit(collector, directory)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def run_dashboard(directory, refresh=5, once=False, as_json=False):
    if once or as_json or not sys.stdin.isatty() or not sys.stdout.isatty():
        snapshot = collect_snapshot(directory)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2) if as_json else text_snapshot(snapshot))
        return 0
    try:
        import curses
    except ImportError:
        print("Terminal UI requires curses; use 'ctm ui --once'.", file=sys.stderr)
        return 2
    try:
        curses.wrapper(interactive, directory, refresh)
        return 0
    except KeyboardInterrupt:
        return 0
    except curses.error as exc:
        print(f"Terminal UI unavailable ({type(exc).__name__}); use 'ctm ui --once'.", file=sys.stderr)
        return 2
