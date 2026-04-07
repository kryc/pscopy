#!/usr/bin/env python3
"""Scrape psxdatacenter.com game lists into a SQLite database."""

import argparse
import re
import sqlite3

import requests
from bs4 import BeautifulSoup

SOURCES = [
    # (url, platform, region)
    ("https://psxdatacenter.com/ntsc-u_list.html", "PS1", "NTSC-U"),
    ("https://psxdatacenter.com/pal_list.html", "PS1", "PAL"),
    ("https://psxdatacenter.com/ntsc-j_list.html", "PS1", "NTSC-J"),
    ("https://psxdatacenter.com/psx2/ulist2.html", "PS2", "NTSC-U"),
    ("https://psxdatacenter.com/psx2/pal_list2.html", "PS2", "PAL"),
    ("https://psxdatacenter.com/psx2/ntsc-j_list2.html", "PS2", "NTSC-J"),
]

DISC_RE = re.compile(r"\[\s*(\d+)\s*DISCS?\s*\]", re.IGNORECASE)
SERIAL_RE = re.compile(r"[A-Z]{2,5}[\s._-]?\d{3,5}")


def init_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS games (
            serial TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            platform TEXT NOT NULL,
            region TEXT NOT NULL,
            languages TEXT,
            disc_number INTEGER DEFAULT 1,
            total_discs INTEGER DEFAULT 1
        )"""
    )
    conn.commit()
    return conn


_ROMAN = re.compile(r"^[IVXLCDM]+$")
_ACRONYMS = {
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "XIII", "XIV", "XV",
    "XVI", "XX", "XXX", "RPG", "NBA", "NFL", "NHL", "MLB", "NCAA", "FIFA",
    "WWE", "WWF", "WCW", "NWO", "ECW", "UFC", "MLS", "PGA", "ATP", "WTA",
    "USA", "UK", "EU", "DJ", "MC", "MTV", "ESPN", "HBO", "BBC", "TV",
    "3D", "2D", "GT", "GX", "DX", "EX", "HD", "VR", "RC", "FX", "XL",
    "DDR", "PS1", "PS2", "PSP", "GBA", "NES", "SNES", "DLC",
    "ATV", "BMX", "MX", "F1", "GP", "WRC",
    "LEGO", "GTA", "DBZ",
}


def _smart_title(text):
    """Title-case a string, preserving acronyms and roman numerals."""
    words = text.split()
    result = []
    for word in words:
        upper = word.upper()
        # Keep known acronyms uppercase
        if upper in _ACRONYMS or _ROMAN.match(upper):
            result.append(upper)
        # Keep words with internal punctuation patterns like 2K10, X-O, etc.
        elif re.match(r"^\d+[A-Z]", word) or re.match(r"^[A-Z]\d", word):
            result.append(word)
        else:
            # Title-case the word, but handle hyphenated parts
            parts = word.split("-")
            titled = "-".join(
                p.capitalize() if p.upper() not in _ACRONYMS else p.upper()
                for p in parts
            )
            result.append(titled)
    return " ".join(result)


def clean_title(raw):
    """Strip disc count tags and Includes: descriptions, then smart title-case."""
    title = DISC_RE.sub("", raw)
    # Remove "Includes: ..." suffix (sometimes after a dash separator)
    title = re.sub(r"\s*-?\s*Includes:\s*.*$", "", title, flags=re.IGNORECASE)
    title = title.strip().rstrip("-").strip()
    # Only convert if the title is all-caps (preserve already mixed-case titles)
    if title and title == title.upper():
        title = _smart_title(title)
    return title


def parse_serials(raw):
    """Split a serial cell into individual serial numbers."""
    # Serials are space-separated. Some have typos like "SLPS-SLPS-25652"
    # so we find all valid serial patterns.
    return SERIAL_RE.findall(raw.strip())


def resolve_frameset(url, verbose=False):
    """If the page is a frameset, follow the last frame to get the content page."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    frames = soup.find_all("frame")
    if not frames:
        return resp  # Not a frameset, return as-is

    # The last frame contains the game list
    content_src = frames[-1].get("src", "")
    if not content_src:
        return resp

    # Build absolute URL relative to the frameset page
    base = url.rsplit("/", 1)[0] + "/"
    content_url = base + content_src
    if verbose:
        print(f"  Following frame to {content_url} ...")
    content_resp = requests.get(content_url, timeout=60)
    content_resp.raise_for_status()
    return content_resp


def scrape_page(url, platform, region, conn, verbose=False):
    """Scrape a single list page and insert games into the database."""
    if verbose:
        print(f"Fetching {url} ...")
    resp = resolve_frameset(url, verbose)

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.find_all("tr")

    count = 0
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        serial_text = cells[1].get_text(strip=True)
        title_text = cells[2].get_text(strip=True)
        lang_text = cells[3].get_text(strip=True)

        # Skip header rows
        if serial_text.upper() == "SERIAL" or not serial_text:
            continue

        serials = parse_serials(serial_text)
        if not serials:
            continue

        # Detect multi-disc
        disc_match = DISC_RE.search(title_text)
        total_discs = int(disc_match.group(1)) if disc_match else 1
        title = clean_title(title_text)

        if not title:
            continue

        # Assign disc numbers to serials
        for i, serial in enumerate(serials):
            serial = serial.upper().strip()
            disc_num = (i + 1) if total_discs > 1 else 1
            # Clamp disc_num to total_discs
            if disc_num > total_discs:
                disc_num = total_discs

            conn.execute(
                """INSERT OR REPLACE INTO games
                   (serial, title, platform, region, languages, disc_number, total_discs)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (serial, title, platform, region, lang_text, disc_num, total_discs),
            )
            count += 1

    conn.commit()
    if verbose:
        print(f"  Inserted {count} entries from {platform} {region}")
    return count


def main():
    parser = argparse.ArgumentParser(description="Scrape psxdatacenter.com into SQLite")
    parser.add_argument("--db", default="games.db", help="Output database path (default: games.db)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show progress")
    args = parser.parse_args()

    conn = init_db(args.db)
    total = 0

    for url, platform, region in SOURCES:
        total += scrape_page(url, platform, region, conn, args.verbose)

    if args.verbose:
        print(f"Done. Total entries: {total}")

    conn.close()


if __name__ == "__main__":
    main()
