"""Standalone tkinter progress window for code indexing.

Reads JSON lines from stdin with progress updates:
  {"phase": "parse", "current": 5, "total": 20, "detail": "parsing foo.py"}
  {"done": true, "summary": "Indexed 50 files in 12.3s"}

Auto-closes after completion or if stdin is closed.
"""

import json
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

# How long to show the "done" state before auto-closing (ms)
DONE_DISPLAY_MS = 2500


class ProgressWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Code Index")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(False)

        # Window size and centering
        w, h = 420, 140
        sx = self.root.winfo_screenwidth()
        sy = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sx - w) // 2}+{(sy - h) // 2}")

        # Prevent closing via X button while running (user can still Alt+F4)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- Layout ---
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        self.title_label = ttk.Label(frame, text="Indexing project...", font=("Segoe UI", 11, "bold"))
        self.title_label.pack(anchor="w")

        self.progress = ttk.Progressbar(frame, length=380, mode="indeterminate")
        self.progress.pack(pady=(8, 4), fill="x")
        self.progress.start(20)  # Start indeterminate animation

        self.detail_label = ttk.Label(frame, text="Starting...", font=("Segoe UI", 9), foreground="#666")
        self.detail_label.pack(anchor="w")

        self._determinate = False
        self._done = False
        self._phase_start_time = {}  # phase -> timestamp when first progress arrived
        self._phase_first_current = {}  # phase -> first non-zero current value

        # Start reading stdin in a background thread
        self._reader = threading.Thread(target=self._read_stdin, daemon=True)
        self._reader.start()

    def _read_stdin(self):
        """Read JSON lines from stdin and schedule UI updates."""
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if msg.get("done"):
                    summary = msg.get("summary", "Complete!")
                    self.root.after(0, self._show_done, summary)
                    return
                else:
                    self.root.after(0, self._update, msg)
        except (EOFError, OSError):
            pass
        finally:
            # stdin closed — close window after a short delay
            self.root.after(500, self._force_close)

    def _update(self, msg):
        """Update the progress bar and labels from a message dict."""
        phase = msg.get("phase", "")
        current = msg.get("current", 0)
        total = msg.get("total", 0)
        detail = msg.get("detail", "")

        # Phase display names
        phase_titles = {
            "init": "Initializing...",
            "discover": "Discovering files...",
            "parse": "Parsing files...",
            "embed": "Generating embeddings...",
            "store": "Saving to database...",
            "cleanup": "Cleaning up...",
        }
        title = phase_titles.get(phase, "Indexing...")
        self.title_label.config(text=title)

        # Track phase timing for ETR calculation
        now = time.monotonic()
        if phase not in self._phase_start_time and current > 0:
            self._phase_start_time[phase] = now
            self._phase_first_current[phase] = current

        # Switch between indeterminate (animated) and determinate (percentage)
        if total > 0 and current > 0 and current < total:
            # Mid-progress — show determinate bar
            if not self._determinate:
                self.progress.stop()
                self.progress.config(mode="determinate", maximum=100)
                self._determinate = True
            pct = min(100, int((current / total) * 100))
            self.progress["value"] = pct

            etr = self._estimate_remaining(phase, current, total, now)
            self.detail_label.config(text=f"{current}/{total}  {etr}")
        elif total > 0 and current == total:
            # Phase finished — snap to 100%
            if not self._determinate:
                self.progress.stop()
                self.progress.config(mode="determinate", maximum=100)
                self._determinate = True
            self.progress["value"] = 100
            self.detail_label.config(text=f"{current}/{total}  done")
            # Reset timing for next phase
            self._phase_start_time.pop(phase, None)
            self._phase_first_current.pop(phase, None)
        else:
            # Phase starting (current=0) or no total — use animated bar
            if self._determinate:
                self.progress.config(mode="indeterminate")
                self.progress.start(20)
                self._determinate = False
            self.detail_label.config(text=detail or "Working...")

    def _estimate_remaining(self, phase, current, total, now):
        """Calculate estimated time remaining string."""
        start = self._phase_start_time.get(phase)
        first_current = self._phase_first_current.get(phase, 0)
        if not start or current <= first_current:
            return ""
        elapsed = now - start
        progress_made = current - first_current
        remaining_items = total - current
        if progress_made <= 0:
            return ""
        rate = elapsed / progress_made
        remaining_secs = rate * remaining_items
        return self._format_time(remaining_secs)

    @staticmethod
    def _format_time(seconds):
        """Format seconds into a human-readable ETR string."""
        seconds = max(0, int(seconds))
        if seconds < 5:
            return "almost done"
        if seconds < 60:
            return f"~{seconds}s remaining"
        minutes = seconds // 60
        secs = seconds % 60
        if minutes < 60:
            if secs >= 15:
                return f"~{minutes}m {secs}s remaining"
            return f"~{minutes}m remaining"
        hours = minutes // 60
        mins = minutes % 60
        return f"~{hours}h {mins}m remaining"

    def _show_done(self, summary):
        """Show completion state and schedule auto-close."""
        self._done = True
        self.title_label.config(text="Indexing complete!")
        if self._determinate:
            self.progress["value"] = 100
        else:
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=100, value=100)
        self.detail_label.config(text=summary)
        self.root.after(DONE_DISPLAY_MS, self._force_close)

    def _on_close(self):
        """Allow closing the window at any time."""
        self._force_close()

    def _force_close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ProgressWindow().run()
