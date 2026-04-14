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
ANSI_ESC_RE = re.compile(r'\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07|\([B0UK]|\)[B0UK]|[>=<])|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

LOG_DIR = os.path.expanduser("~/.pscopy")


def sanitize_filename(name):
    name = name.replace(" & ", " And ")
    return UNSAFE_CHARS.sub("-", name)


def msf_to_seconds(m, s, _f=0):
    return int(m) * 60 + int(s)


def seconds_to_display(secs):
    return f"{secs // 60:02d}:{secs % 60:02d}"


def region_code(region_str):
    """Map DB region to short code."""
    mapping = {"NTSC-U": "US", "PAL": "UK", "NTSC-J": "JP"}
    return mapping.get(region_str, region_str)


def make_non_blocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


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
        self._setup_colors()

    def _setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(self.COLOR_NORMAL, -1, -1)
        curses.init_pair(self.COLOR_GOOD, curses.COLOR_GREEN, -1)
        curses.init_pair(self.COLOR_BAD, curses.COLOR_RED, -1)
        curses.init_pair(self.COLOR_WARN, curses.COLOR_YELLOW, -1)
        curses.init_pair(self.COLOR_INPUT, curses.COLOR_WHITE, curses.COLOR_BLACK)

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

    def draw(self):
        try:
            self.stdscr.erase()
            h, w = self.stdscr.getmaxyx()
            if h < 10 or w < 40:
                self.stdscr.addstr(0, 0, "Terminal too small")
                self.stdscr.refresh()
                return

            # Top section box
            title = " pscopy "
            self.stdscr.addstr(0, 0, "┌─" + title + "─" * (w - len(title) - 4) + "─┐")

            row = 1
            # Status
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, f"Status: {self.status}"[:w - 4],
                               curses.color_pair(self.status_color) | curses.A_BOLD)
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Device / Media / Output
            dev_media = f"Device: {self.device}         Media: {self.media}         Output: {self.args.output}"
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, dev_media[:w - 4])
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Game info
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, f"Game:   {self.game_info}"[:w - 4])
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Progress bar inside top box
            label = "Progress: "
            bar_width = w - len(label) - 16
            if bar_width > 10:
                filled = int(self.progress * bar_width)
                bar = "▓" * filled + "░" * (bar_width - filled)
                pct = f" {self.progress * 100:5.1f}%"
                pt = f"  {self.progress_text}" if self.progress_text else ""
                line = f"{label}[{bar}]{pct}{pt}"
                self.stdscr.addstr(row, 0, "│ ")
                self.stdscr.addstr(row, 2, line[:w - 4])
                self.stdscr.addstr(row, w - 1, "│")
            else:
                self.stdscr.addstr(row, 0, "│" + " " * (w - 2) + "│")
            row += 1

            # Close top section box
            self.stdscr.addstr(row, 0, "└" + "─" * (w - 2) + "┘")
            row += 1

            # Disc Input box
            input_title = " Disc Input "
            self.stdscr.addstr(row, 0, "┌─" + input_title + "─" * (w - len(input_title) - 4) + "─┐")
            row += 1

            # Prompt line (always visible)
            prompt_text = self.input_prompt if self.input_prompt else "Enter game code or custom title: "
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, prompt_text[:w - 4])
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Input value line with highlight
            input_text = f" > {self.input_value}"
            pad = " " * max(0, w - 4 - len(input_text))
            if self.input_active:
                self.stdscr.addstr(row, 0, "│")
                self.stdscr.addstr(row, 1, (input_text + pad)[:w - 3],
                                   curses.color_pair(self.COLOR_INPUT) | curses.A_BOLD)
                self.stdscr.addstr(row, w - 1, "│")
            else:
                self.stdscr.addstr(row, 0, "│" + (" " * (w - 2)) + "│")
            row += 1

            # Disc name line
            disc_label = f"Disc name: {self.disc_name}" if self.disc_name else "Disc name:"
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, disc_label[:w - 4])
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Close input box
            self.stdscr.addstr(row, 0, "└" + "─" * (w - 2) + "┘")
            row += 1

            # Log box
            log_title = " Log "
            log_rows = h - row - 2  # reserve top border + bottom border
            if log_rows < 1:
                log_rows = 1
            self.stdscr.addstr(row, 0, "┌─" + log_title + "─" * (w - len(log_title) - 4) + "─┐")
            row += 1

            visible = self.log_lines[-log_rows:]
            for i in range(log_rows):
                if row + i >= h - 1:
                    break
                if i < len(visible):
                    self.stdscr.addstr(row + i, 0, "│ ")
                    self.stdscr.addstr(row + i, 2, visible[i][:w - 4])
                    self.stdscr.addstr(row + i, w - 1, "│")
                else:
                    self.stdscr.addstr(row + i, 0, "│" + " " * (w - 2) + "│")
            row += log_rows

            # Close log box — last row, handle curses.error on bottom-right cell
            if row < h:
                try:
                    self.stdscr.addstr(row, 0, "└" + "─" * (w - 2) + "┘")
                except curses.error:
                    pass

        except curses.error:
            pass

        self.stdscr.refresh()

    def get_input(self, prompt, timeout_ms=None):
        """Get user input within the curses TUI.

        If timeout_ms is set, use non-blocking input with that refresh interval
        and return None if no input is submitted (call again to continue).
        """
        curses.flushinp()
        self.input_prompt = prompt
        self.input_value = ""
        self.input_active = True
        self.draw()

        if timeout_ms is None:
            self.stdscr.nodelay(False)
        else:
            self.stdscr.nodelay(True)
        curses.echo()

        result = []
        while True:
            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1
            if ch == -1:
                if timeout_ms is not None:
                    curses.noecho()
                    return None  # No input yet
                continue
            if ch in (curses.KEY_ENTER, 10, 13):
                break
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if result:
                    result.pop()
                    self.input_value = "".join(result)
                    self.draw()
            elif 32 <= ch < 127:
                result.append(chr(ch))
                self.input_value = "".join(result)
                self.draw()

        curses.noecho()
        self.stdscr.nodelay(True)
        self.input_active = False
        self.draw()
        return "".join(result).strip()

    def get_input_nonblocking(self, prompt):
        """Collect input character by character without blocking.

        Call repeatedly from an event loop. Returns the final string when
        the user presses Enter, or None while still typing.
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
            if ch in (curses.KEY_ENTER, 10, 13):
                self.input_active = False
                result = "".join(self._input_buf).strip()
                self._input_buf = []
                self.draw()
                return result
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if self._input_buf:
                    self._input_buf.pop()
                    self.input_value = "".join(self._input_buf)
                    self.draw()
            elif 32 <= ch < 127:
                self._input_buf.append(chr(ch))
                self.input_value = "".join(self._input_buf)
                self.draw()

    def get_confirm(self, prompt, default=True):
        """Get Y/n confirmation via single keypress."""
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


def lookup_serial(db, serial):
    """Look up a serial in the database."""
    row = db.execute(
        "SELECT title, platform, region, languages, disc_number, total_discs FROM games WHERE serial = ?",
        (serial.upper(),),
    ).fetchone()
    if row:
        return {
            "title": row[0],
            "platform": row[1],
            "region": row[2],
            "languages": row[3],
            "disc_number": row[4],
            "total_discs": row[5],
        }
    return None


def detect_media_type(device_path):
    """Detect whether disc is CD (PS1) or DVD (PS2) via udev properties."""
    # Ensure udev properties are up to date after disc change
    subprocess.run(["udevadm", "settle", "--timeout=5"], capture_output=True, check=False)
    ctx = pyudev.Context()
    for dev in ctx.list_devices(subsystem="block", DEVNAME=device_path):
        # Only check media-specific properties, not drive capabilities
        if dev.get("ID_CDROM_MEDIA_DVD") == "1":
            return "DVD"
        if dev.get("ID_CDROM_MEDIA_CD") == "1":
            return "CD"
    return None


def _preview_resolve(tui, text):
    """Try to resolve input to a game title for display. Returns title or raw text."""
    candidate = text.strip()
    # Normalize serial without hyphen
    if SERIAL_RE.match(candidate):
        serial = candidate.upper()
        if '-' not in serial:
            serial = serial[:4] + '-' + serial[4:]
        info = lookup_serial(tui.db, serial)
        if info:
            return info["title"]
    # Try 5-digit shortcut
    if re.match(r'^\d{5}$', candidate) and tui.last_region:
        prefixes = tui.db.execute(
            "SELECT DISTINCT substr(serial, 1, 5) || '-' FROM games WHERE region = ?",
            (tui.last_region,),
        ).fetchall()
        for (prefix,) in prefixes:
            serial = f"{prefix}{candidate}"
            info = lookup_serial(tui.db, serial)
            if info:
                return info["title"]
    return candidate


def wait_for_disc(tui, device_path):
    """Wait for a disc insertion event using pyudev, or detect one already present.

    While waiting, the user can type a game code / title. If they submit
    input before the disc arrives it is stored in tui.pre_input.
    """
    tui.set_status("Waiting for disc... (type code/title now or after insert)", CursesTUI.COLOR_NORMAL)
    tui.media = "--"
    tui.game_info = "--"
    tui.set_progress(0.0)
    tui.pre_input = None
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
    _confirming = False  # True when waiting for Y/N after resolve
    while True:
        # Poll udev with a short timeout so we can check keyboard regularly
        device = monitor.poll(timeout=0.1)

        # Check for keyboard input
        try:
            ch = tui.stdscr.getch()
            while ch != -1:
                if _confirming:
                    # Waiting for Y/N confirmation
                    if ch in (curses.KEY_ENTER, 10, 13, ord("y"), ord("Y")):
                        # Accepted
                        _confirming = False
                        tui.input_prompt = "Enter game code or custom title: "
                        tui.input_active = False
                        tui.input_value = ""
                        tui.add_log(f"Confirmed: {tui.disc_name}")
                        tui.draw()
                    elif ch in (ord("n"), ord("N")):
                        # Rejected — re-enter
                        _confirming = False
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
                    if ch in (curses.KEY_ENTER, 10, 13):
                        tui.pre_input = "".join(tui._input_buf).strip()
                        tui._input_buf = []
                        if tui.pre_input:
                            resolved_name = _preview_resolve(tui, tui.pre_input)
                            tui.disc_name = resolved_name
                            tui.add_log(f"Detected: {resolved_name}")
                            # Switch to confirmation mode
                            _confirming = True
                            tui.input_prompt = f"Detected: {resolved_name}"
                            tui.input_value = "Y/n?"
                            tui.input_active = True
                            # Flush remaining input to avoid auto-accept
                            curses.flushinp()
                            tui.draw()
                            break
                        else:
                            tui.input_active = False
                        tui.draw()
                    elif ch in (curses.KEY_BACKSPACE, 127, 8):
                        if tui._input_buf:
                            tui._input_buf.pop()
                            tui.input_value = "".join(tui._input_buf)
                            tui.draw()
                    elif 32 <= ch < 127:
                        tui._input_buf.append(chr(ch))
                        tui.input_value = "".join(tui._input_buf)
                        tui.draw()
                else:
                    # Input already confirmed, re-activate on any printable key
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

        if device and device.properties.get('ACTION') == "change" and device.properties.get('DEVNAME') == device_path:
            media = detect_media_type(device_path)
            if media:
                tui.media = media
                # If confirming, accept what was detected
                if _confirming:
                    _confirming = False
                    tui.input_active = False
                # Don't auto-submit mid-typing — leave input alone for user to finish
                tui.set_status(f"Disc detected ({media})", CursesTUI.COLOR_WARN)
                return True

    return False


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
    errors_found = False

    with open(log_path, "w") as logf:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        make_non_blocking(proc.stdout.fileno())

        buf = b""
        while proc.poll() is None:
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                try:
                    chunk = proc.stdout.read(4096)
                except OSError:
                    chunk = None
                if chunk:
                    buf += chunk
                    # Process complete lines (split on \n and \r)
                    while b"\n" in buf or b"\r" in buf:
                        for sep in (b"\n", b"\r"):
                            idx = buf.find(sep)
                            if idx >= 0:
                                line_bytes = buf[:idx]
                                buf = buf[idx + 1:]
                                break
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        line = ANSI_ESC_RE.sub("", line).strip()
                        if not line:
                            continue
                        logf.write(line + "\n")
                        logf.flush()

                        if "L-EC error" in line.lower():
                            errors_found = True

                        # Parse total length
                        track_m = CDRDAO_TRACK_RE.search(line)
                        if track_m:
                            total_secs = msf_to_seconds(
                                track_m.group(1), track_m.group(2)
                            )
                            tui.add_log(line)

                        # Parse position
                        pos_m = CDRDAO_POS_RE.match(line)
                        if pos_m and total_secs > 0:
                            cur_secs = msf_to_seconds(
                                pos_m.group(1), pos_m.group(2)
                            )
                            frac = cur_secs / total_secs if total_secs else 0
                            tui.set_progress(
                                frac,
                                f"{seconds_to_display(cur_secs)} / {seconds_to_display(total_secs)}",
                            )
                        elif "Copying" in line or "error" in line.lower():
                            tui.add_log(line)

            # Let curses refresh
            tui.draw()

        # Drain remaining output
        try:
            remaining = proc.stdout.read()
            if remaining:
                for line in remaining.decode("utf-8", errors="replace").splitlines():
                    line = ANSI_ESC_RE.sub("", line).strip()
                    if line:
                        logf.write(line + "\n")
                        if "L-EC error" in line.lower():
                            errors_found = True
        except OSError:
            pass

    ret = proc.returncode
    tui.add_log(f"cdrdao exited with code {ret}")

    # Also check log for L-EC errors
    try:
        with open(log_path) as f:
            content = f.read()
        if "l-ec error" in content.lower():
            errors_found = True
    except OSError:
        pass

    # Generate .cue from .toc
    if os.path.exists(toc_path):
        cue_path = os.path.join(output_dir, f"{basename}.cue")
        try:
            subprocess.run(
                ["toc2cue", toc_path, cue_path],
                capture_output=True, check=False,
            )
            tui.add_log("Generated .cue from .toc")
        except FileNotFoundError:
            tui.add_log("toc2cue not found, skipping .cue generation")

    return not errors_found


def dump_ps2(tui, device_path, basename, output_dir):
    """Dump PS2 disc using dd."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{os.path.basename(basename)}.log")
    iso_path = os.path.join(output_dir, f"{basename}.iso")

    total_bytes = get_disc_size(device_path)

    cmd = [
        "dd",
        f"if={device_path}",
        f"of={iso_path}",
        "status=progress",
        "conv=sync,noerror",
    ]

    tui.add_log(f"$ {' '.join(cmd)}")

    errors_found = False

    with open(log_path, "w") as logf:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        make_non_blocking(proc.stderr.fileno())

        buf = b""
        while proc.poll() is None:
            ready, _, _ = select.select([proc.stderr], [], [], 0.2)
            if ready:
                try:
                    chunk = proc.stderr.read(4096)
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
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        line = ANSI_ESC_RE.sub("", line).strip()
                        if not line:
                            continue
                        logf.write(line + "\n")
                        logf.flush()

                        if "error" in line.lower():
                            errors_found = True

                        bytes_m = DD_BYTES_RE.search(line)
                        if bytes_m and total_bytes > 0:
                            copied = int(bytes_m.group(1))
                            frac = copied / total_bytes
                            mb_copied = copied / (1024 * 1024)
                            mb_total = total_bytes / (1024 * 1024)
                            tui.set_progress(
                                frac,
                                f"{mb_copied:.0f} MB / {mb_total:.0f} MB",
                            )
                        elif "error" in line.lower():
                            tui.add_log(line)

            tui.draw()

        # Drain remaining
        try:
            remaining = proc.stderr.read()
            if remaining:
                for line in remaining.decode("utf-8", errors="replace").splitlines():
                    line = ANSI_ESC_RE.sub("", line).strip()
                    if line:
                        logf.write(line + "\n")
                        tui.add_log(line)
                        if "error" in line.lower():
                            errors_found = True
        except OSError:
            pass

    ret = proc.returncode
    tui.add_log(f"dd exited with code {ret}")

    # Also check log
    try:
        with open(log_path) as f:
            content = f.read()
        if "error" in content.lower():
            errors_found = True
    except OSError:
        pass

    return not errors_found


def update_file_references(filepath, old_name, new_name):
    """Update FILE references in .cue or .toc files."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r") as f:
            content = f.read()
        content = content.replace(old_name, new_name)
        with open(filepath, "w") as f:
            f.write(content)
    except OSError:
        pass


def eject_disc(device_path):
    """Eject the disc."""
    try:
        subprocess.run(["eject", device_path], capture_output=True, check=False)
    except FileNotFoundError:
        pass


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


def resolve_user_input(tui, user_input):
    """Resolve user input into (serial, title, region, disc_number, total_discs).

    Returns None if the user cancelled/skipped.
    """
    # If user entered exactly 5 digits, try prefixes from the last region
    if re.match(r'^\d{5}$', user_input) and tui.last_region:
        digits = user_input
        prefixes = tui.db.execute(
            "SELECT DISTINCT substr(serial, 1, 5) || '-' FROM games WHERE region = ?",
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

    serial = None
    title = None
    region = None
    disc_number = 1
    total_discs = 1

    if SERIAL_RE.match(user_input):
        serial = user_input.upper()
        # Normalize: insert hyphen if missing (e.g. SLES54211 -> SLES-54211)
        if '-' not in serial:
            serial = serial[:4] + '-' + serial[4:]
        info = lookup_serial(tui.db, serial)
        if info:
            title = info["title"]
            region = region_code(info["region"])
            disc_number = info["disc_number"]
            total_discs = info["total_discs"]
            tui.game_info = f"{title}  [{serial}]  Disc {disc_number}/{total_discs}  ({region})"
            tui.add_log(f"Found in DB: {title} ({info['region']})")
            tui.last_region = info["region"]
        else:
            tui.add_log(f"Serial {serial} not found in database")
            user_input = tui.get_input("Enter full code or game title: ")
            if not user_input:
                return None
            if SERIAL_RE.match(user_input):
                serial = user_input.upper()
                if '-' not in serial:
                    serial = serial[:4] + '-' + serial[4:]
                info = lookup_serial(tui.db, serial)
                if info:
                    title = info["title"]
                    region = region_code(info["region"])
                    disc_number = info["disc_number"]
                    total_discs = info["total_discs"]
                    tui.game_info = f"{title}  [{serial}]  Disc {disc_number}/{total_discs}  ({region})"
                    tui.add_log(f"Found in DB: {title} ({info['region']})")
                    tui.last_region = info["region"]
                else:
                    tui.add_log(f"Serial {serial} not found in database")
                    title = tui.get_input("Enter game title: ")
                    if not title:
                        return None
                    region = tui.get_input("Enter region (e.g., US, UK, JP): ")
                    if not region:
                        region = "US"
                    tui.game_info = f"{title}  [{serial}]  ({region})"
            else:
                serial = None
                title = user_input
                region = tui.get_input("Enter region (e.g., US, UK, JP): ")
                if not region:
                    region = "US"
                tui.game_info = f"{title}  ({region})"
    else:
        title = user_input
        region = tui.get_input("Enter region (e.g., US, UK, JP): ")
        if not region:
            region = "US"
        tui.game_info = f"{title}  ({region})"

    return serial, title, region, disc_number, total_discs


def rename_dump(output_dir, media, temp_base, final_base, tui):
    """Rename temp dump files to their final names."""
    if media == "CD":
        for ext in (".bin", ".cue", ".toc"):
            old_path = os.path.join(output_dir, f"{temp_base}{ext}")
            new_path = os.path.join(output_dir, f"{final_base}{ext}")
            if os.path.exists(old_path):
                if ext in (".cue", ".toc"):
                    update_file_references(old_path, f"{temp_base}.bin", f"{final_base}.bin")
                os.rename(old_path, new_path)
        tui.add_log(f"Output: {final_base}.bin/.cue/.toc")
    else:
        old_path = os.path.join(output_dir, f"{temp_base}.iso")
        new_path = os.path.join(output_dir, f"{final_base}.iso")
        if os.path.exists(old_path):
            os.rename(old_path, new_path)
        tui.add_log(f"Output: {final_base}.iso")


def run_backup_cycle(tui):
    """Run one complete backup cycle: detect → dump+identify → rename → eject."""
    output_dir = tui.args.output

    # Wait for disc
    if not wait_for_disc(tui, tui.device):
        return False  # User quit

    media = tui.media

    # Start dumping immediately with a temp name
    temp_base = "_pscopy_temp"
    tui.set_status(f"Dumping {media} disc...", CursesTUI.COLOR_WARN)
    tui.set_progress(0.0)
    os.makedirs(output_dir, exist_ok=True)

    dump_result = {}

    def _dump_thread():
        if media == "CD":
            dump_result["good"] = dump_ps1(tui, tui.device, temp_base, output_dir)
        else:
            dump_result["good"] = dump_ps2(tui, tui.device, temp_base, output_dir)

    dump_thread = threading.Thread(target=_dump_thread, daemon=True)
    dump_thread.start()

    # Use pre-entered input if available, otherwise collect while dumping
    user_input = getattr(tui, 'pre_input', None) or None
    if user_input:
        tui.disc_name = _preview_resolve(tui, user_input)
        tui.draw()
        tui.add_log(f"Using pre-entered: {user_input}")
    else:
        tui.add_log("Type game code or title while dump runs...")
        while dump_thread.is_alive():
            result = tui.get_input_nonblocking("Enter game code or custom title: ")
            if result is not None:
                user_input = result
                tui.disc_name = _preview_resolve(tui, user_input)
                tui.draw()
                break
            time.sleep(0.05)
            tui.draw()

    # If input was entered before dump finished, resolve now while dump continues
    if user_input and dump_thread.is_alive():
        # Resolve and confirm while dump runs in background
        while True:
            resolved = resolve_user_input(tui, user_input)
            if resolved is None:
                user_input = None
                break
            serial, title, region, disc_number, total_discs = resolved
            tui.disc_name = title
            tui.draw()
            if tui.get_confirm(f"Use \"{title}\"? [Y/n] "):
                break
            user_input = tui.get_input("Enter game code or custom title: ")
            if not user_input:
                user_input = None
                break
        # Wait for dump to finish while keeping UI alive
        while dump_thread.is_alive():
            time.sleep(0.1)
            tui.draw()
        dump_thread.join()
        if user_input is None:
            tui.set_status("No input, skipping...", CursesTUI.COLOR_WARN)
            for ext in (".bin", ".cue", ".toc", ".iso"):
                p = os.path.join(output_dir, f"{temp_base}{ext}")
                if os.path.exists(p):
                    os.remove(p)
            eject_disc(tui.device)
            return True
        # Already resolved and confirmed — skip to quality check
        good = dump_result.get("good", False)
        quality = "[!]" if good else "[b]"
        if good:
            tui.set_status("Good dump!", CursesTUI.COLOR_GOOD)
            tui.add_log("Dump verified: no errors detected")
        else:
            tui.set_status("BAD DUMP (errors detected)", CursesTUI.COLOR_BAD)
            tui.add_log("BAD DUMP: errors found in log")
        tui.set_progress(1.0, "Complete")
        final_base = build_filename(title, region, serial, disc_number, total_discs, quality)
        rename_dump(output_dir, media, temp_base, final_base, tui)
        eject_disc(tui.device)
        tui.add_log("Disc ejected")
        return True

    # If dump finished before input, wait for input normally
    dump_thread.join()

    if user_input is None and not tui.input_active:
        # No typing started — show blocking prompt
        user_input = tui.get_input("Enter game code or custom title: ")
        if user_input:
            tui.disc_name = _preview_resolve(tui, user_input)
            tui.draw()
    elif user_input is None and tui.input_active:
        # User was mid-typing when dump finished — keep their buffer, switch to blocking
        tui.stdscr.nodelay(False)
        while True:
            try:
                ch = tui.stdscr.getch()
            except curses.error:
                continue
            if ch in (curses.KEY_ENTER, 10, 13):
                user_input = "".join(tui._input_buf).strip()
                tui.input_active = False
                tui._input_buf = []
                if user_input:
                    tui.disc_name = _preview_resolve(tui, user_input)
                tui.draw()
                break
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if tui._input_buf:
                    tui._input_buf.pop()
                    tui.input_value = "".join(tui._input_buf)
                    tui.draw()
            elif 32 <= ch < 127:
                tui._input_buf.append(chr(ch))
                tui.input_value = "".join(tui._input_buf)
                tui.draw()
        tui.stdscr.nodelay(True)

    if not user_input:
        tui.set_status("No input, skipping...", CursesTUI.COLOR_WARN)
        # Clean up temp files
        for ext in (".bin", ".cue", ".toc", ".iso"):
            p = os.path.join(output_dir, f"{temp_base}{ext}")
            if os.path.exists(p):
                os.remove(p)
        eject_disc(tui.device)
        return True

    while True:
        resolved = resolve_user_input(tui, user_input)
        if resolved is None:
            tui.set_status("Skipped...", CursesTUI.COLOR_WARN)
            for ext in (".bin", ".cue", ".toc", ".iso"):
                p = os.path.join(output_dir, f"{temp_base}{ext}")
                if os.path.exists(p):
                    os.remove(p)
            eject_disc(tui.device)
            return True

        serial, title, region, disc_number, total_discs = resolved
        tui.disc_name = title
        tui.draw()

        # Confirm resolved title
        if tui.get_confirm(f"Use \"{title}\"? [Y/n] "):
            break

        # User rejected — let them re-enter
        user_input = tui.get_input("Enter game code or custom title: ")
        if not user_input:
            tui.set_status("No input, skipping...", CursesTUI.COLOR_WARN)
            for ext in (".bin", ".cue", ".toc", ".iso"):
                p = os.path.join(output_dir, f"{temp_base}{ext}")
                if os.path.exists(p):
                    os.remove(p)
            eject_disc(tui.device)
            return True

    # Determine quality tag
    good = dump_result.get("good", False)
    quality = "[!]" if good else "[b]"
    if good:
        tui.set_status("Good dump!", CursesTUI.COLOR_GOOD)
        tui.add_log("Dump verified: no errors detected")
    else:
        tui.set_status("BAD DUMP (errors detected)", CursesTUI.COLOR_BAD)
        tui.add_log("BAD DUMP: errors found in log")

    tui.set_progress(1.0, "Complete")

    # Rename temp files to final names
    final_base = build_filename(title, region, serial, disc_number, total_discs, quality)
    rename_dump(output_dir, media, temp_base, final_base, tui)

    # Eject
    eject_disc(tui.device)
    tui.add_log("Disc ejected")

    return True


def main_curses(stdscr, args):
    curses.curs_set(0)
    stdscr.nodelay(True)

    tui = CursesTUI(stdscr, args)

    try:
        while True:
            if not run_backup_cycle(tui):
                break
            # Brief pause before next cycle
            tui.set_status("Ready for next disc (q to quit)...", CursesTUI.COLOR_NORMAL)
            tui.draw()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        tui.close()


def main():
    parser = argparse.ArgumentParser(description="PS1/PS2 disc backup tool")
    parser.add_argument("-o", "--output", required=True, help="Output directory for dumps")
    parser.add_argument("--db", default="games.db", help="Path to game database (default: games.db)")
    parser.add_argument("--device", default="/dev/cdrom", help="Optical drive device (default: /dev/cdrom)")
    args = parser.parse_args()

    # Validate
    if not os.path.exists(args.db):
        print(f"Error: Database not found: {args.db}", file=sys.stderr)
        print("Run scrape_db.py first to create the game database.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    curses.wrapper(main_curses, args)


if __name__ == "__main__":
    main()
