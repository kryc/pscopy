#!/usr/bin/env python3
"""PS1/PS2 disc backup tool with curses TUI."""

import argparse
import curses
import fcntl
import os
import re
import select
import sqlite3
import subprocess
import sys
import threading
import time
import warnings

import pyudev

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Serial regex: covers SLUS/SCUS, SLES/SCES, SLPS/SCPS/SLPM/SCPM, etc.
SERIAL_RE = re.compile(r"^S[LC][UEPM][SM]-?\d{5}$", re.IGNORECASE)
# cdrdao progress: "Copying data track N (MODE): start MM:SS:FF, length MM:SS:FF"
CDRDAO_TRACK_RE = re.compile(r"length\s+(\d+):(\d+):(\d+)")
# cdrdao current position: "MM:SS:FF" at start of line (possibly with \r)
CDRDAO_POS_RE = re.compile(r"(\d+):(\d+):(\d+)")
# dd progress: "12345678 bytes"
DD_BYTES_RE = re.compile(r"(\d+)\s+bytes")
# Filesystem-unsafe characters
UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')
# ANSI escape sequences and control characters
ANSI_ESC_RE = re.compile(
    r'\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07|\([B0UK]|\)[B0UK]|[>=<])'
    r'|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]'
)
# Orphaned CSI fragments (ESC was split off at a line boundary)
ORPHAN_CSI_RE = re.compile(
    r'\[[\d;]*[A-Za-z]'
    r'|[\d;]{2,}[HfABCDJKSTm]'
    r'|\?\d+[hl]'
)

TEMP_BASE = "_pscopy_temp"
LOG_DIR = os.path.expanduser("~/.pscopy")
DUMP_EXTENSIONS = (".bin", ".cue", ".toc", ".iso")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def sanitize_filename(name):
    name = name.replace(" & ", " And ")
    name = name.replace("'", "")
    return UNSAFE_CHARS.sub("-", name)


def msf_to_seconds(m, s, _f=0):
    return int(m) * 60 + int(s)


def seconds_to_display(secs):
    return f"{secs // 60:02d}:{secs % 60:02d}"


def region_code(region_str):
    """Map DB region to short code."""
    return {"NTSC-U": "US", "PAL": "UK", "NTSC-J": "JP"}.get(region_str, region_str)


def make_non_blocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def normalize_serial(text):
    """Uppercase and insert hyphen if missing (e.g. SLES54211 -> SLES-54211)."""
    serial = text.strip().upper()
    if "-" not in serial:
        serial = serial[:4] + "-" + serial[4:]
    return serial


def clean_line(raw_bytes):
    """Decode raw bytes and strip ANSI escapes / control characters."""
    text = ANSI_ESC_RE.sub("", raw_bytes.decode("utf-8", errors="replace"))
    text = ORPHAN_CSI_RE.sub("", text)
    return text.strip()


def cleanup_temp_files(output_dir):
    """Remove temporary dump files."""
    for ext in DUMP_EXTENSIONS:
        p = os.path.join(output_dir, f"{TEMP_BASE}{ext}")
        if os.path.exists(p):
            os.remove(p)


def build_filename(title, region, serial=None, disc_number=1, total_discs=1, quality="[!]"):
    """Build the output filename (without extension)."""
    parts = [title]
    if total_discs > 1:
        parts.append(f"(Disc {disc_number})")
    if serial:
        parts.append(f"[{serial}]")
    parts.append(f"({region})")
    parts.append(quality)
    return sanitize_filename(" ".join(parts))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def lookup_serial(db, serial):
    """Look up a serial in the database."""
    row = db.execute(
        "SELECT title, platform, region, languages, disc_number, total_discs "
        "FROM games WHERE serial = ?",
        (serial.upper(),),
    ).fetchone()
    if row:
        return {
            "title": row[0], "platform": row[1], "region": row[2],
            "languages": row[3], "disc_number": row[4], "total_discs": row[5],
        }
    return None


def try_lookup(db, user_input):
    """Try to interpret user_input as a serial and look it up.

    Returns (serial, info_dict) or (None, None).
    """
    if SERIAL_RE.match(user_input):
        serial = normalize_serial(user_input)
        return serial, lookup_serial(db, serial)
    return None, None


# ---------------------------------------------------------------------------
# Hardware helpers
# ---------------------------------------------------------------------------

def detect_media_type(device_path):
    """Detect whether disc is CD or DVD via udev properties."""
    subprocess.run(["udevadm", "settle", "--timeout=5"], capture_output=True, check=False)
    ctx = pyudev.Context()
    for dev in ctx.list_devices(subsystem="block", DEVNAME=device_path):
        if dev.get("ID_CDROM_MEDIA_DVD") == "1":
            return "DVD"
        if dev.get("ID_CDROM_MEDIA_CD") == "1":
            return "CD"
    return None


def get_disc_size(device_path):
    """Get disc size in bytes via blockdev."""
    try:
        result = subprocess.run(
            ["blockdev", "--getsize64", device_path],
            capture_output=True, text=True, check=True,
        )
        return int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0


def eject_disc(device_path):
    """Eject the disc."""
    try:
        subprocess.run(["eject", device_path], capture_output=True, check=False)
    except FileNotFoundError:
        pass


def update_file_references(filepath, old_name, new_name):
    """Update FILE references in .cue or .toc files."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r") as f:
            content = f.read()
        with open(filepath, "w") as f:
            f.write(content.replace(old_name, new_name))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Curses TUI
# ---------------------------------------------------------------------------

class CursesTUI:
    """Full-screen curses TUI for disc backup operations."""

    COLOR_NORMAL = 1
    COLOR_GOOD = 2
    COLOR_BAD = 3
    COLOR_WARN = 4
    COLOR_INPUT = 5

    def __init__(self, stdscr, args):
        self.stdscr = stdscr
        self.args = args
        self.status = "Waiting for disc..."
        self.status_color = self.COLOR_NORMAL
        self.device = args.device
        self.media = "--"
        self.game_info = "--"
        self.progress = 0.0
        self.progress_text = ""
        self.log_lines = []
        self.max_log = 50
        self.input_prompt = "Enter game code or custom title: "
        self.input_value = ""
        self.input_active = False
        self.disc_name = ""
        self.db = sqlite3.connect(args.db)
        self.last_region = ""
        self._input_buf = []
        self._setup_colors()

    def _setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(self.COLOR_NORMAL, -1, -1)
        curses.init_pair(self.COLOR_GOOD, curses.COLOR_GREEN, -1)
        curses.init_pair(self.COLOR_BAD, curses.COLOR_RED, -1)
        curses.init_pair(self.COLOR_WARN, curses.COLOR_YELLOW, -1)
        curses.init_pair(self.COLOR_INPUT, curses.COLOR_WHITE, curses.COLOR_BLACK)

    # -- state setters (auto-redraw) ----------------------------------------

    def set_status(self, text, color=None):
        self.status = text
        if color is not None:
            self.status_color = color
        self.draw()

    def add_log(self, line):
        self.log_lines.append(line)
        if len(self.log_lines) > self.max_log:
            self.log_lines = self.log_lines[-self.max_log:]
        self.draw()

    def set_progress(self, fraction, text=""):
        self.progress = max(0.0, min(1.0, fraction))
        self.progress_text = text
        self.draw()

    # -- drawing helpers -----------------------------------------------------

    def _box_top(self, row, w, title=""):
        if title:
            self.stdscr.addstr(row, 0, "┌─" + title + "─" * (w - len(title) - 4) + "─┐")
        else:
            self.stdscr.addstr(row, 0, "┌" + "─" * (w - 2) + "┐")

    def _box_bottom(self, row, w):
        self.stdscr.addstr(row, 0, "└" + "─" * (w - 2) + "┘")

    def _box_row(self, row, w, text, attr=0):
        self.stdscr.addstr(row, 0, "│ ")
        self.stdscr.addstr(row, 2, text[:w - 4], attr)
        self.stdscr.addstr(row, w - 1, "│")

    def _box_empty(self, row, w):
        self.stdscr.addstr(row, 0, "│" + " " * (w - 2) + "│")

    def draw(self):
        try:
            self.stdscr.erase()
            h, w = self.stdscr.getmaxyx()
            if h < 10 or w < 40:
                self.stdscr.addstr(0, 0, "Terminal too small")
                self.stdscr.refresh()
                return

            # -- Top section box --
            self._box_top(0, w, " pscopy ")
            row = 1

            self._box_row(row, w, f"Status: {self.status}",
                          curses.color_pair(self.status_color) | curses.A_BOLD)
            row += 1

            self._box_row(row, w,
                          f"Device: {self.device}         Media: {self.media}"
                          f"         Output: {self.args.output}")
            row += 1

            self._box_row(row, w, f"Game:   {self.game_info}")
            row += 1

            # Progress bar
            label = "Progress: "
            bar_width = w - len(label) - 16
            if bar_width > 10:
                filled = int(self.progress * bar_width)
                bar = "▓" * filled + "░" * (bar_width - filled)
                pct = f" {self.progress * 100:5.1f}%"
                pt = f"  {self.progress_text}" if self.progress_text else ""
                self._box_row(row, w, f"{label}[{bar}]{pct}{pt}")
            else:
                self._box_empty(row, w)
            row += 1

            self._box_bottom(row, w)
            row += 1

            # -- Disc Input box --
            self._box_top(row, w, " Disc Input ")
            row += 1

            self._box_row(row, w,
                          self.input_prompt or "Enter game code or custom title: ")
            row += 1

            input_text = f" > {self.input_value}"
            pad = " " * max(0, w - 4 - len(input_text))
            if self.input_active:
                self.stdscr.addstr(row, 0, "│")
                self.stdscr.addstr(row, 1, (input_text + pad)[:w - 3],
                                   curses.color_pair(self.COLOR_INPUT) | curses.A_BOLD)
                self.stdscr.addstr(row, w - 1, "│")
            else:
                self._box_empty(row, w)
            row += 1

            disc_label = f"Disc name: {self.disc_name}" if self.disc_name else "Disc name:"
            self._box_row(row, w, disc_label)
            row += 1

            self._box_bottom(row, w)
            row += 1

            # -- Log box --
            log_rows = max(1, h - row - 2)
            self._box_top(row, w, " Log ")
            row += 1

            visible = self.log_lines[-log_rows:]
            for i in range(log_rows):
                if row + i >= h - 1:
                    break
                if i < len(visible):
                    self._box_row(row + i, w, visible[i])
                else:
                    self._box_empty(row + i, w)
            row += log_rows

            if row < h:
                try:
                    self._box_bottom(row, w)
                except curses.error:
                    pass

        except curses.error:
            pass

        self.stdscr.refresh()

    # -- input handling ------------------------------------------------------

    def _process_key(self, ch):
        """Process a single keypress for the active input buffer.

        Returns the submitted string on Enter, or None otherwise.
        """
        if ch in (curses.KEY_ENTER, 10, 13):
            self.input_active = False
            result = "".join(self._input_buf).strip()
            self._input_buf = []
            self.draw()
            return result
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if self._input_buf:
                self._input_buf.pop()
                self.input_value = "".join(self._input_buf)
                self.draw()
        elif 32 <= ch < 127:
            self._input_buf.append(chr(ch))
            self.input_value = "".join(self._input_buf)
            self.draw()
        return None

    def get_input(self, prompt):
        """Blocking input: show prompt, collect keystrokes, return on Enter."""
        curses.flushinp()
        self.input_prompt = prompt
        self.input_value = ""
        self.input_active = True
        self._input_buf = []
        self.draw()

        self.stdscr.nodelay(False)
        while True:
            try:
                ch = self.stdscr.getch()
            except curses.error:
                continue
            result = self._process_key(ch)
            if result is not None:
                self.stdscr.nodelay(True)
                return result

    def get_input_nonblocking(self, prompt):
        """Non-blocking input: call repeatedly from event loop.

        Returns submitted string on Enter, or None while still typing.
        """
        if not self.input_active:
            self.input_prompt = prompt
            self.input_value = ""
            self.input_active = True
            self._input_buf = []
            self.stdscr.nodelay(True)
            self.draw()

        # Drain all available characters
        while True:
            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1
            if ch == -1:
                return None
            result = self._process_key(ch)
            if result is not None:
                return result

    def finish_blocking_input(self):
        """Switch mid-typing nonblocking input to blocking and wait for Enter.

        Returns the submitted string, or None if empty.
        """
        self.stdscr.nodelay(False)
        while True:
            try:
                ch = self.stdscr.getch()
            except curses.error:
                continue
            result = self._process_key(ch)
            if result is not None:
                self.stdscr.nodelay(True)
                return result or None

    def get_confirm(self, prompt):
        """Single-keypress Y/N confirmation."""
        curses.flushinp()
        self.input_prompt = prompt
        self.input_value = "Press Y to accept, N to reject"
        self.input_active = True
        self.draw()

        self.stdscr.nodelay(False)
        curses.noecho()
        while True:
            try:
                ch = self.stdscr.getch()
            except curses.error:
                continue
            if ch in (ord("n"), ord("N")):
                self.input_active = False
                self.input_value = ""
                self.draw()
                return False
            if ch in (curses.KEY_ENTER, 10, 13, ord("y"), ord("Y")):
                self.input_active = False
                self.input_value = ""
                self.draw()
                return True

    def close(self):
        self.db.close()


# ---------------------------------------------------------------------------
# Preview resolve (quick DB lookup for display)
# ---------------------------------------------------------------------------

def _preview_resolve(tui, text):
    """Try to resolve input to a game title for display. Returns title or raw text."""
    candidate = text.strip()
    if SERIAL_RE.match(candidate):
        info = lookup_serial(tui.db, normalize_serial(candidate))
        if info:
            return info["title"]
    if re.match(r"^\d{5}$", candidate) and tui.last_region:
        prefixes = tui.db.execute(
            "SELECT DISTINCT substr(serial, 1, 4) || '-' FROM games WHERE region = ?",
            (tui.last_region,),
        ).fetchall()
        for (prefix,) in prefixes:
            info = lookup_serial(tui.db, f"{prefix}{candidate}")
            if info:
                return info["title"]
    return candidate


# ---------------------------------------------------------------------------
# Wait for disc (with concurrent keyboard input)
# ---------------------------------------------------------------------------

def wait_for_disc(tui, device_path):
    """Wait for a disc insertion event, allowing pre-entry of game code."""
    tui.set_status("Waiting for disc... (type code/title now or after insert)",
                   CursesTUI.COLOR_NORMAL)
    tui.media = "--"
    tui.game_info = "--"
    tui.set_progress(0.0)
    tui.pre_input = None
    tui.pre_confirmed = False
    tui.disc_name = ""
    tui.input_prompt = "Enter game code or custom title: "
    tui.input_active = True
    tui._input_buf = []
    tui.input_value = ""
    tui.draw()

    # Check if a disc is already in the drive
    media = detect_media_type(device_path)
    if media:
        tui.media = media
        tui.set_status(f"Disc detected ({media})", CursesTUI.COLOR_WARN)
        return True

    ctx = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(ctx)
    monitor.filter_by(subsystem="block")

    tui.stdscr.nodelay(True)
    confirming = False

    while True:
        device = monitor.poll(timeout=0.1)

        try:
            ch = tui.stdscr.getch()
            while ch != -1:
                if confirming:
                    if ch in (curses.KEY_ENTER, 10, 13, ord("y"), ord("Y")):
                        confirming = False
                        tui.input_prompt = "Enter game code or custom title: "
                        tui.input_active = False
                        tui.input_value = ""
                        tui.add_log(f"Confirmed: {tui.disc_name}")
                        tui.draw()
                    elif ch in (ord("n"), ord("N")):
                        confirming = False
                        tui.pre_input = None
                        tui.disc_name = ""
                        tui.input_prompt = "Enter game code or custom title: "
                        tui.input_active = True
                        tui._input_buf = []
                        tui.input_value = ""
                        tui.draw()

                elif ch == ord("q") and not tui.input_active:
                    return False

                elif tui.input_active:
                    result = tui._process_key(ch)
                    if result is not None and result:
                        tui.pre_input = result
                        resolved_name = _preview_resolve(tui, result)
                        tui.disc_name = resolved_name
                        tui.add_log(f"Detected: {resolved_name}")
                        confirming = True
                        tui.input_prompt = f"Detected: {resolved_name}"
                        tui.input_value = "Y/n?"
                        tui.input_active = True
                        curses.flushinp()
                        tui.draw()
                        break
                    elif result is not None:
                        # Empty Enter — just deactivate
                        pass

                else:
                    # Input already confirmed; re-activate on printable key
                    if 32 <= ch < 127:
                        tui.input_active = True
                        tui._input_buf = [chr(ch)]
                        tui.input_value = chr(ch)
                        tui.pre_input = None
                        tui.disc_name = ""
                        tui.draw()

                ch = tui.stdscr.getch()
        except curses.error:
            pass

        if (device
                and device.properties.get("ACTION") == "change"
                and device.properties.get("DEVNAME") == device_path):
            media = detect_media_type(device_path)
            if media:
                tui.media = media
                if confirming:
                    confirming = False
                    tui.input_active = False
                tui.set_status(f"Disc detected ({media})", CursesTUI.COLOR_WARN)
                return True

    return False


# ---------------------------------------------------------------------------
# Subprocess monitoring (shared by dump_ps1 / dump_ps2)
# ---------------------------------------------------------------------------

def _run_monitored_process(tui, cmd, read_stderr, log_path, line_callback):
    """Run a subprocess with non-blocking output monitoring.

    Args:
        read_stderr: If True, monitor stderr (dd). Otherwise merge stderr→stdout (cdrdao).
        line_callback: Called with each cleaned line. Returns True if the line indicates an error.

    Returns True if no errors detected.
    """
    errors_found = False
    env = os.environ.copy()
    env["TERM"] = "dumb"

    with open(log_path, "w") as logf:
        if read_stderr:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, stdin=subprocess.DEVNULL, start_new_session=True,
                env=env,
            )
            read_fd = proc.stderr
        else:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, stdin=subprocess.DEVNULL, start_new_session=True,
                env=env,
            )
            read_fd = proc.stdout

        make_non_blocking(read_fd.fileno())

        buf = b""
        while proc.poll() is None:
            ready, _, _ = select.select([read_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = read_fd.read(4096)
                except OSError:
                    chunk = None
                if chunk:
                    buf += chunk
                    while b"\n" in buf or b"\r" in buf:
                        for sep in (b"\n", b"\r"):
                            idx = buf.find(sep)
                            if idx >= 0:
                                line_bytes = buf[:idx]
                                buf = buf[idx + 1:]
                                break
                        line = clean_line(line_bytes)
                        if not line:
                            continue
                        logf.write(line + "\n")
                        logf.flush()
                        if line_callback(line):
                            errors_found = True
            tui.draw()

        # Drain remaining output
        try:
            remaining = read_fd.read()
            if remaining:
                for raw_line in remaining.splitlines():
                    line = clean_line(raw_line)
                    if line:
                        logf.write(line + "\n")
                        if line_callback(line):
                            errors_found = True
        except OSError:
            pass

    tui.add_log(f"{cmd[0]} exited with code {proc.returncode}")

    # Double-check full log for errors
    try:
        with open(log_path) as f:
            content = f.read().lower()
        error_needle = "error" if read_stderr else "l-ec error"
        if error_needle in content:
            errors_found = True
    except OSError:
        pass

    return not errors_found


# ---------------------------------------------------------------------------
# Dump functions
# ---------------------------------------------------------------------------

def dump_ps1(tui, device_path, basename, output_dir):
    """Dump PS1 disc using cdrdao."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{os.path.basename(basename)}.log")
    bin_path = os.path.join(output_dir, f"{basename}.bin")
    toc_path = os.path.join(output_dir, f"{basename}.toc")

    cmd = [
        "cdrdao", "read-cd", "--read-raw",
        "--driver", "generic-mmc:0x20000",
        "--device", device_path,
        "--datafile", bin_path,
        toc_path,
    ]
    tui.add_log(f"$ {' '.join(cmd)}")

    total_secs = 0

    def on_line(line):
        nonlocal total_secs
        is_error = "l-ec error" in line.lower()

        track_m = CDRDAO_TRACK_RE.search(line)
        if track_m:
            total_secs = msf_to_seconds(track_m.group(1), track_m.group(2))
            tui.add_log(line)

        pos_m = CDRDAO_POS_RE.match(line)
        if pos_m and total_secs > 0:
            cur = msf_to_seconds(pos_m.group(1), pos_m.group(2))
            tui.set_progress(
                cur / total_secs,
                f"{seconds_to_display(cur)} / {seconds_to_display(total_secs)}",
            )
        elif "Copying" in line or "error" in line.lower():
            tui.add_log(line)

        return is_error

    good = _run_monitored_process(tui, cmd, read_stderr=False,
                                  log_path=log_path, line_callback=on_line)

    # Generate .cue from .toc
    if os.path.exists(toc_path):
        cue_path = os.path.join(output_dir, f"{basename}.cue")
        try:
            subprocess.run(["toc2cue", toc_path, cue_path],
                           capture_output=True, check=False)
            tui.add_log("Generated .cue from .toc")
        except FileNotFoundError:
            tui.add_log("toc2cue not found, skipping .cue generation")

    return good


def dump_ps2(tui, device_path, basename, output_dir):
    """Dump PS2 disc using dd."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{os.path.basename(basename)}.log")
    iso_path = os.path.join(output_dir, f"{basename}.iso")
    total_bytes = get_disc_size(device_path)

    cmd = [
        "dd", f"if={device_path}", f"of={iso_path}",
        "status=progress", "conv=sync,noerror",
    ]
    tui.add_log(f"$ {' '.join(cmd)}")

    def on_line(line):
        is_error = "error" in line.lower()
        bytes_m = DD_BYTES_RE.search(line)
        if bytes_m and total_bytes > 0:
            copied = int(bytes_m.group(1))
            tui.set_progress(
                copied / total_bytes,
                f"{copied / (1024 * 1024):.0f} MB / {total_bytes / (1024 * 1024):.0f} MB",
            )
        elif is_error:
            tui.add_log(line)
        return is_error

    return _run_monitored_process(tui, cmd, read_stderr=True,
                                  log_path=log_path, line_callback=on_line)


# ---------------------------------------------------------------------------
# Game resolution
# ---------------------------------------------------------------------------

def resolve_user_input(tui, user_input):
    """Resolve user input into (serial, title, region, disc_number, total_discs).

    Returns None if the user cancelled/skipped.
    """
    # 5-digit shortcut: try region-specific prefixes
    if re.match(r"^\d{5}$", user_input) and tui.last_region:
        digits = user_input
        prefixes = tui.db.execute(
            "SELECT DISTINCT substr(serial, 1, 4) || '-' FROM games WHERE region = ?",
            (tui.last_region,),
        ).fetchall()
        for (prefix,) in prefixes:
            candidate = f"{prefix}{digits}"
            if lookup_serial(tui.db, candidate):
                user_input = candidate
                tui.add_log(f"Matched {user_input}")
                break
        else:
            tui.add_log(f"No match for {digits} in {tui.last_region}")

    # Try serial lookup (with one retry if not found)
    serial, info = try_lookup(tui.db, user_input)
    if serial and not info:
        tui.add_log(f"Serial {serial} not found in database")
        user_input = tui.get_input("Enter full code or game title: ")
        if not user_input:
            return None
        new_serial, info = try_lookup(tui.db, user_input)
        if new_serial:
            serial = new_serial
        else:
            serial = None  # User entered a title, not a serial
        if serial and not info:
            tui.add_log(f"Serial {serial} not found in database")

    # Found in DB
    if info:
        title = info["title"]
        region = region_code(info["region"])
        disc_number = info["disc_number"]
        total_discs = info["total_discs"]
        tui.game_info = (f"{title}  [{serial}]  "
                         f"Disc {disc_number}/{total_discs}  ({region})")
        tui.add_log(f"Found in DB: {title} ({info['region']})")
        tui.last_region = info["region"]
        return serial, title, region, disc_number, total_discs

    # Not found — manual entry
    if serial:
        title = tui.get_input("Enter game title: ")
        if not title:
            return None
    else:
        title = user_input

    region = tui.get_input("Enter region (e.g., US, UK, JP): ")
    if not region:
        region = "US"

    if serial:
        tui.game_info = f"{title}  [{serial}]  ({region})"
    else:
        tui.game_info = f"{title}  ({region})"
    return serial, title, region, 1, 1


def rename_dump(output_dir, media, final_base, tui):
    """Rename temp dump files to their final names."""
    if media == "CD":
        for ext in (".bin", ".cue", ".toc"):
            old_path = os.path.join(output_dir, f"{TEMP_BASE}{ext}")
            new_path = os.path.join(output_dir, f"{final_base}{ext}")
            if os.path.exists(old_path):
                if ext in (".cue", ".toc"):
                    update_file_references(old_path, f"{TEMP_BASE}.bin",
                                           f"{final_base}.bin")
                os.rename(old_path, new_path)
        tui.add_log(f"Output: {final_base}.bin/.cue/.toc")
    else:
        old_path = os.path.join(output_dir, f"{TEMP_BASE}.iso")
        new_path = os.path.join(output_dir, f"{final_base}.iso")
        if os.path.exists(old_path):
            os.rename(old_path, new_path)
        tui.add_log(f"Output: {final_base}.iso")


# ---------------------------------------------------------------------------
# Backup cycle
# ---------------------------------------------------------------------------

def _resolve_and_confirm(tui, user_input):
    """Resolve user input and get Y/N confirmation. Returns resolved tuple or None."""
    while True:
        resolved = resolve_user_input(tui, user_input)
        if resolved is None:
            return None
        _, title, *_ = resolved
        tui.disc_name = title
        tui.draw()
        if tui.get_confirm(f'Use "{title}"? [Y/n] '):
            return resolved
        user_input = tui.get_input("Enter game code or custom title: ")
        if not user_input:
            return None


def _collect_user_input(tui, dump_thread):
    """Collect user input during or after the dump. Returns string or None."""
    # Pre-entered during wait_for_disc
    user_input = getattr(tui, "pre_input", None) or None
    if user_input:
        tui.disc_name = _preview_resolve(tui, user_input)
        tui.draw()
        tui.add_log(f"Using pre-entered: {user_input}")
        return user_input

    # Collect while dump runs
    tui.add_log("Type game code or title while dump runs...")
    while dump_thread.is_alive():
        result = tui.get_input_nonblocking("Enter game code or custom title: ")
        if result is not None:
            tui.disc_name = _preview_resolve(tui, result)
            tui.draw()
            return result
        time.sleep(0.05)
        tui.draw()

    # Dump finished — user was mid-typing → switch to blocking
    if tui.input_active:
        result = tui.finish_blocking_input()
        if result:
            tui.disc_name = _preview_resolve(tui, result)
            tui.draw()
        return result

    # No typing started — blocking prompt
    result = tui.get_input("Enter game code or custom title: ")
    if result:
        tui.disc_name = _preview_resolve(tui, result)
        tui.draw()
    return result or None


def _finish_cycle(tui, dump_result, resolved, output_dir, media):
    """Quality check → rename → eject."""
    serial, title, region, disc_number, total_discs = resolved
    good = dump_result.get("good", False)
    quality = "[!]" if good else "[b]"

    if good:
        tui.set_status("Good dump!", CursesTUI.COLOR_GOOD)
        tui.add_log("Dump verified: no errors detected")
    else:
        tui.set_status("BAD DUMP (errors detected)", CursesTUI.COLOR_BAD)
        tui.add_log("BAD DUMP: errors found in log")

    tui.set_progress(1.0, "Complete")
    final_base = build_filename(title, region, serial, disc_number,
                                total_discs, quality)
    rename_dump(output_dir, media, final_base, tui)
    eject_disc(tui.device)
    tui.add_log("Disc ejected")


def run_backup_cycle(tui):
    """Run one complete backup cycle: detect → dump + identify → rename → eject."""
    output_dir = tui.args.output

    if not wait_for_disc(tui, tui.device):
        return False  # User quit

    media = tui.media

    # Start dumping immediately with a temp name
    tui.set_status(f"Dumping {media} disc...", CursesTUI.COLOR_WARN)
    tui.set_progress(0.0)
    os.makedirs(output_dir, exist_ok=True)

    dump_result = {}

    def _dump_thread():
        if media == "CD":
            dump_result["good"] = dump_ps1(tui, tui.device, TEMP_BASE, output_dir)
        else:
            dump_result["good"] = dump_ps2(tui, tui.device, TEMP_BASE, output_dir)

    thread = threading.Thread(target=_dump_thread, daemon=True)
    thread.start()

    # Collect user input (during or after dump)
    user_input = _collect_user_input(tui, thread)

    # If input entered while dump still running, resolve now
    pre_confirmed = getattr(tui, 'pre_confirmed', False)
    if user_input and thread.is_alive():
        if pre_confirmed:
            # Already confirmed during wait — just resolve without re-asking
            resolved = resolve_user_input(tui, user_input)
        else:
            resolved = _resolve_and_confirm(tui, user_input)
        while thread.is_alive():
            time.sleep(0.1)
            tui.draw()
        thread.join()

        if resolved is None:
            cleanup_temp_files(output_dir)
            eject_disc(tui.device)
            return True
        _finish_cycle(tui, dump_result, resolved, output_dir, media)
        return True

    # Dump finished first
    thread.join()

    if not user_input:
        tui.set_status("No input, skipping...", CursesTUI.COLOR_WARN)
        cleanup_temp_files(output_dir)
        eject_disc(tui.device)
        return True

    if pre_confirmed:
        resolved = resolve_user_input(tui, user_input)
    else:
        resolved = _resolve_and_confirm(tui, user_input)
    if resolved is None:
        tui.set_status("Skipped...", CursesTUI.COLOR_WARN)
        cleanup_temp_files(output_dir)
        eject_disc(tui.device)
        return True

    _finish_cycle(tui, dump_result, resolved, output_dir, media)
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main_curses(stdscr, args):
    curses.curs_set(0)
    stdscr.nodelay(True)
    tui = CursesTUI(stdscr, args)
    try:
        while True:
            if not run_backup_cycle(tui):
                break
            tui.set_status("Ready for next disc (q to quit)...",
                           CursesTUI.COLOR_NORMAL)
            tui.draw()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        tui.close()


def main():
    parser = argparse.ArgumentParser(description="PS1/PS2 disc backup tool")
    parser.add_argument("-o", "--output", required=True,
                        help="Output directory for dumps")
    parser.add_argument("--db", default="games.db",
                        help="Path to game database (default: games.db)")
    parser.add_argument("--device", default="/dev/cdrom",
                        help="Optical drive device (default: /dev/cdrom)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: Database not found: {args.db}", file=sys.stderr)
        print("Run scrape_db.py first to create the game database.",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    curses.wrapper(main_curses, args)


if __name__ == "__main__":
    main()
