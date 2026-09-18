"""CLI entry points for the Video Publisher.

* ``bottle-net gui`` - start the GUI (local web app) and the scheduler.
* ``bottle-net publisher jobs`` - list uploads in the terminal.
* ``bottle-net publisher run`` - run the scheduler without the GUI.

The GUI is the main interface; these commands are optional.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import webbrowser
from pathlib import Path

from bottle_net.utils.console import UI

EXIT_OK = 0
EXIT_FAILURE = 1
DEFAULT_PORT = 8765
SESSION_FILE = "gui-session.json"


def _open_app(args: argparse.Namespace):  # noqa: ANN202 - imported lazily
    from bottle_net.publisher.app import PublisherApp, setup_file_logging

    app = PublisherApp.open(Path(args.data_dir) if getattr(args, "data_dir", None) else None)
    setup_file_logging(app.paths)
    return app


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def run_gui(args: argparse.Namespace, ui: UI) -> int:
    """Start the Video Publisher GUI and keep it running until Ctrl+C."""
    from bottle_net.publisher.server import PublisherServer

    app = _open_app(args)
    session_file = app.paths.root / SESSION_FILE
    server = PublisherServer(app, port=args.port)
    try:
        server.start()
    except OSError:
        app.close()
        # Probably already running: reopen it with the saved private link.
        try:
            saved = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if saved.get("port") == args.port and saved.get("launch_url"):
            ui.info(f"Bottle Net is already running on port {args.port}; opening it.")
            if not args.no_browser:
                webbrowser.open(saved["launch_url"])
            return EXIT_OK
        ui.error(f"Port {args.port} is already in use.",
                 hint="Close the program using it, or start Bottle Net on another port: bottle-net gui --port 8766")
        return EXIT_FAILURE

    scheduler = app.start_scheduler()
    _write_private(session_file, json.dumps({"port": server.port, "launch_url": server.launch_url}))
    ui.header("Video Publisher")
    ui.field("Address", server.base_url, width=10)
    ui.field("Data", str(app.paths.root), width=10)
    ui.field("Scheduler", "running" if scheduler else "not started (another Bottle Net window runs it)", width=10)
    ui.blank()
    if args.no_browser:
        # The link contains a private sign-in key; it is only shown to you here.
        ui.note(f"Open this private link in your browser: {server.launch_url}")
    else:
        webbrowser.open(server.launch_url)
        ui.info("Opened Bottle Net in your browser.")
    ui.note("Keep this window open for scheduled uploads. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ui.blank()
        ui.info("Stopping Bottle Net. Unfinished uploads resume the next time it starts.")
    finally:
        server.stop()
        app.close()
        session_file.unlink(missing_ok=True)
    return EXIT_OK


def run_scheduler(args: argparse.Namespace, ui: UI) -> int:
    """Run only the scheduler (no GUI) until Ctrl+C."""
    app = _open_app(args)
    if not app.start_scheduler():
        ui.error("Another Bottle Net publisher is already running the scheduler.")
        app.close()
        return EXIT_FAILURE
    ui.info(f"Scheduler running (data: {app.paths.root}). Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ui.info("Scheduler stopped.")
    finally:
        app.close()
    return EXIT_OK


def list_jobs(args: argparse.Namespace, ui: UI) -> int:
    """Print the upload queue (one line per platform upload) to stdout."""
    app = _open_app(args)
    try:
        zone = app.settings().zone
        rows = []
        for job in app.db.list_jobs():
            when = (job.scheduled_at or job.created_at).astimezone(zone).strftime("%Y-%m-%d %H:%M")
            for pj in job.platform_jobs:
                rows.append(f"#{job.id:<5} {when}  {pj.platform.display_name:<9} {pj.status.value:<10} {job.title}")
        if not rows:
            ui.note("No uploads yet. Start the GUI with: bottle-net gui")
        for row in rows:
            sys.stdout.write(row + "\n")
    finally:
        app.close()
    return EXIT_OK
