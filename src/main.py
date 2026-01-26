#!/usr/bin/env python3
# src/main.py

import sys
import os, sys, traceback

from pathlib import Path
from core.application import Application
from core.cli import CommandLine
from replicator.replicator import Replicator

# ---------------------------------------------------------------------------
# Customization and start of the application
# ---------------------------------------------------------------------------

name = "Replicator"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def has_option():
    return any(arg.startswith('--') for arg in sys.argv)

def runtime_setup():
    try:
        if getattr(sys, "frozen", False):
            os.chdir(Path(sys.executable).resolve().parent)
        else:
            os.chdir(Path(__file__).resolve().parent)

        log_dir = Path.home() / "Library" / "Logs" / "Replicator"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "replicator-crash.log"

        def excepthook(t, e, tb):
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("\n" + "="*80 + "\n")
                traceback.print_exception(t, e, tb, file=f)
            sys.__excepthook__(t, e, tb)

        sys.excepthook = excepthook
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Start of the application
# ---------------------------------------------------------------------------

def start_app():
    # Perform any runtime setup
    runtime_setup()

    # Create application and register it with QApplication
    app = Application(name,sys.argv)

    # Create main window and register it with Application
    win = Replicator()
    app.set_mainWindow(win)

    # All other code gets app via QApplication.instance()
    sys.exit(app.exec_())

def start_cli():
    # Perform any runtime setup
    runtime_setup()

    # Create command line application and register it with QApplication
    cli = CommandLine(name,sys.argv)

    # Create main command line handler and register it with CommandLine
    handler = Replicator()
    handler.cli(cli)

    # All other code gets app via QApplication.instance()
    sys.exit(cli.exec())

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if has_option():
        start_cli()
    else:
        start_app()
