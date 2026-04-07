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
import time

import pyudev

# Serial regex: covers SLUS/SCUS, SLES/SCES, SLPS/SCPS/SLPM/SCPM, etc.
SERIAL_RE = re.compile(r"^S[LC][UEPM][SM]-\d{5}$", re.IGNORECASE)
# cdrdao progress: "Copying data track N (MODE): start MM:SS:FF, length MM:SS:FF"
CDRDAO_TRACK_RE = re.compile(r"length\s+(\d+):(\d+):(\d+)")
# cdrdao current position: "MM:SS:FF" at start of line (possibly with \r)
CDRDAO_POS_RE = re.compile(r"(\d+):(\d+):(\d+)")
# dd progress: "12345678 bytes" 
DD_BYTES_RE = re.compile(r"(\d+)\s+bytes")
# Filesystem-unsafe characters
UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')

LOG_DIR = os.path.expanduser("~/.pscopy")


def sanitize_filename(name):
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
        self.input_prompt = ""
        self.input_value = ""
        self.input_active = False
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
        curses.init_pair(self.COLOR_INPUT, curses.COLOR_CYAN, -1)

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

            # Border
            title = " pscopy "
            self.stdscr.addstr(0, 0, "┌─" + title + "─" * (w - len(title) - 4) + "─┐")

            row = 1
            # Status
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, f"Status: {self.status}"[:w - 4],
                               curses.color_pair(self.status_color) | curses.A_BOLD)
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Device / Media
            dev_media = f"Device: {self.device}         Media: {self.media}"
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, dev_media[:w - 4])
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Game info
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, f"Game:   {self.game_info}"[:w - 4])
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Blank
            self.stdscr.addstr(row, 0, "│" + " " * (w - 2) + "│")
            row += 1

            # Progress bar
            bar_width = w - 8  # margins for "│ [" + "] │"
            if bar_width > 20:
                filled = int(self.progress * bar_width)
                bar = "▓" * filled + "░" * (bar_width - filled)
                pct = f" {self.progress * 100:5.1f}%"
                pt = f"  {self.progress_text}" if self.progress_text else ""
                line = f" [{bar}]{pct}{pt}"
                self.stdscr.addstr(row, 0, "│")
                self.stdscr.addstr(row, 1, line[:w - 3])
                self.stdscr.addstr(row, w - 1, "│")
            else:
                self.stdscr.addstr(row, 0, "│" + " " * (w - 2) + "│")
            row += 1

            # Blank
            self.stdscr.addstr(row, 0, "│" + " " * (w - 2) + "│")
            row += 1

            # Input area
            if self.input_active and self.input_prompt:
                inp = f" > {self.input_prompt}{self.input_value}"
                self.stdscr.addstr(row, 0, "│")
                self.stdscr.addstr(row, 1, inp[:w - 3],
                                   curses.color_pair(self.COLOR_INPUT))
                self.stdscr.addstr(row, w - 1, "│")
            else:
                self.stdscr.addstr(row, 0, "│" + " " * (w - 2) + "│")
            row += 1

            # Blank
            self.stdscr.addstr(row, 0, "│" + " " * (w - 2) + "│")
            row += 1

            # Log area header
            self.stdscr.addstr(row, 0, "│ ")
            self.stdscr.addstr(row, 2, "Log:", curses.A_UNDERLINE)
            self.stdscr.addstr(row, w - 1, "│")
            row += 1

            # Log lines — fill remaining space
            log_rows = h - row - 1  # reserve 1 for bottom border
            if log_rows > 0:
                visible = self.log_lines[-log_rows:]
                for i, line_text in enumerate(visible):
                    if row + i >= h - 1:
                        break
                    self.stdscr.addstr(row + i, 0, "│  ")
                    self.stdscr.addstr(row + i, 3, line_text[:w - 5])
                    self.stdscr.addstr(row + i, w - 1, "│")
                # Fill empty log rows
                for i in range(len(visible), log_rows):
                    if row + i >= h - 1:
                        break
                    self.stdscr.addstr(row + i, 0, "│" + " " * (w - 2) + "│")

            # Bottom border — writing to the last cell raises curses.error
            # because the cursor can't advance past the screen, so catch it.
            try:
                self.stdscr.addstr(h - 1, 0, "└" + "─" * (w - 2) + "┘")
            except curses.error:
                pass

        except curses.error:
            pass

        self.stdscr.refresh()

    def get_input(self, prompt):
        """Get user input within the curses TUI."""
        self.input_prompt = prompt
        self.input_value = ""
        self.input_active = True
        self.draw()

        self.stdscr.nodelay(False)
        curses.echo()

        result = []
        while True:
            try:
                ch = self.stdscr.getch()
            except curses.error:
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

    def get_confirm(self, prompt, default=True):
        """Get Y/n confirmation."""
        resp = self.get_input(prompt)
        if not resp:
            return default
        return resp.lower().startswith("y")

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


def wait_for_disc(tui, device_path):
    """Wait for a disc insertion event using pyudev, or detect one already present."""
    tui.set_status("Waiting for disc...", CursesTUI.COLOR_NORMAL)
    tui.media = "--"
    tui.game_info = "--"
    tui.set_progress(0.0)
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

    # Set a short poll timeout so we can check for user input (q to quit)
    tui.stdscr.nodelay(True)
    for device in iter(monitor.poll, None):
        # Check for quit
        try:
            ch = tui.stdscr.getch()
            if ch == ord("q"):
                return False
        except curses.error:
            pass

        if device.action == "change" and device.device_node == device_path:
            media = detect_media_type(device_path)
            if media:
                tui.media = media
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
            bufsize=0,
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
                    line = line.strip()
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
            bufsize=0,
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
                    line = line.strip()
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
    parts.append(f"({region})")
    if serial:
        parts.append(f"[{serial}]")
    parts.append(quality)
    return sanitize_filename(" ".join(parts))


def run_backup_cycle(tui):
    """Run one complete backup cycle: detect → identify → dump → rename → eject."""
    output_dir = tui.args.output

    # Wait for disc
    if not wait_for_disc(tui, tui.device):
        return False  # User quit

    media = tui.media

    # Prompt for game code or custom title
    user_input = tui.get_input("Enter game code or custom title: ")
    if not user_input:
        tui.set_status("No input, skipping...", CursesTUI.COLOR_WARN)
        eject_disc(tui.device)
        return True

    # If user entered exactly 5 digits, try prefixes from the last region
    if re.match(r'^\d{5}$', user_input) and tui.last_region:
        digits = user_input
        prefixes = tui.db.execute(
            "SELECT DISTINCT substr(serial, 1, 5) || '-' FROM games WHERE region = ?",
            (tui.last_region,),
        ).fetchall()
        found_info = None
        for (prefix,) in prefixes:
            candidate = f"{prefix}{digits}"
            found_info = lookup_serial(tui.db, candidate)
            if found_info:
                user_input = candidate
                tui.add_log(f"Matched {user_input}")
                break
        if not found_info:
            tui.add_log(f"No match for {digits} in {tui.last_region}")

    # Determine if serial or custom title
    serial = None
    title = None
    region = None
    disc_number = 1
    total_discs = 1

    if SERIAL_RE.match(user_input):
        serial = user_input.upper()
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
                tui.set_status("No input, skipping...", CursesTUI.COLOR_WARN)
                eject_disc(tui.device)
                return True
            if SERIAL_RE.match(user_input):
                serial = user_input.upper()
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
                        tui.set_status("No title entered, skipping...", CursesTUI.COLOR_WARN)
                        eject_disc(tui.device)
                        return True
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
        # Custom title
        title = user_input
        region = tui.get_input("Enter region (e.g., US, UK, JP): ")
        if not region:
            region = "US"
        tui.game_info = f"{title}  ({region})"

    tui.draw()

    # Confirm
    if not tui.get_confirm("Dump? [Y/n]: "):
        tui.set_status("Skipped by user", CursesTUI.COLOR_WARN)
        eject_disc(tui.device)
        return True

    # Construct temp basename (before quality tag)
    temp_base = build_filename(title, region, serial, disc_number, total_discs, "").rstrip()
    tui.set_status(f"Dumping {media} disc...", CursesTUI.COLOR_WARN)
    tui.set_progress(0.0)

    # Dump based on media type: CD → cdrdao (BIN/CUE), DVD → dd (ISO)
    os.makedirs(output_dir, exist_ok=True)
    if media == "CD":
        good = dump_ps1(tui, tui.device, temp_base, output_dir)
    else:
        good = dump_ps2(tui, tui.device, temp_base, output_dir)

    # Determine quality tag
    quality = "[!]" if good else "[b]"
    if good:
        tui.set_status("Good dump!", CursesTUI.COLOR_GOOD)
        tui.add_log("Dump verified: no errors detected")
    else:
        tui.set_status("BAD DUMP (errors detected)", CursesTUI.COLOR_BAD)
        tui.add_log("BAD DUMP: errors found in log")

    tui.set_progress(1.0, "Complete")

    # Build final name and rename
    final_base = build_filename(title, region, serial, disc_number, total_discs, quality)

    if media == "CD":
        # Rename .bin, .cue, .toc and update FILE references
        for ext in (".bin", ".cue", ".toc"):
            old_path = os.path.join(output_dir, f"{temp_base}{ext}")
            new_path = os.path.join(output_dir, f"{final_base}{ext}")
            if os.path.exists(old_path):
                # Update internal FILE refs before renaming
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
