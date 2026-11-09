#!/usr/bin/env python3
"""
bing_wallpaper.py - Download Bing daily wallpapers and set them per monitor.

The main monitor gets today's wallpaper, the second monitor yesterday's,
the third the one from 2 days ago, and so on. An optional overlay with the
wallpaper title and details is drawn in a configurable corner.

Cross-platform:
  - macOS   : per-monitor via System Events (osascript)
  - Windows : per-monitor via IDesktopWallpaper (needs `pip install comtypes`,
              otherwise falls back to setting today's image on all monitors)
  - Linux   : KDE Plasma (per-monitor via qdbus), XFCE (per-monitor via
              xfconf-query), GNOME/Cinnamon/Budgie (single image via gsettings),
              feh as a generic X11 fallback

Only hard dependency: Pillow (for the overlay). Everything else is stdlib.

Configuration lives in config.json next to this script (auto-created on first
run, or pass --config PATH). See README.md for all options.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("bing-wallpaper")

BING_API = "https://www.bing.com/HPImageArchive.aspx?format=js&idx={idx}&n={n}&mkt={mkt}"
BING_BASE = "https://www.bing.com"
MAX_PER_REQUEST = 8  # the Bing API returns at most 8 images per call
USER_AGENT = "Mozilla/5.0 (compatible; bing-wallpaper-script)"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"

# Descriptions and allowed values for every option live in config.schema.json
# (JSON Schema format - editors like VS Code use it for tooltips, autocomplete
# and validation). config.json itself stays clean: only real options.
DEFAULT_CONFIG = {
    "$schema": "./config.schema.json",

    # Bing market/region to download wallpapers from, e.g. "en-US", "en-GB",
    # "de-DE", "fr-FR", "ja-JP", "ro-RO", ...
    "market": "en-US",

    # Requested download resolution. "UHD" = 3840x2160 (4K). If unavailable,
    # the script falls back to UHD, then "1920x1080".
    "resolution": "UHD",

    # Where wallpapers are stored. "~" is expanded.
    "download_dir": "~/.bing-wallpapers",

    # How many daily images to fetch. "auto" = one per detected monitor.
    "days": "auto",

    # Optional explicit mapping of monitor -> day offset (0 = today,
    # 1 = yesterday, ...). null = automatic [0, 1, 2, ...].
    # Example: [0, 0, 3] shows today on monitors 1-2 and 3-days-ago on 3.
    "day_offsets": None,

    # Delete downloaded images older than this many days (0 = keep forever).
    "keep_days": 14,

    # Set to false to only download images without changing the wallpaper.
    "set_wallpaper": True,

    "schedule": {
        # Hour of day (0-23, local time) from which the script starts looking
        # for TODAY's new wallpaper. Before this hour it always checks; once
        # today's wallpaper has been downloaded AND set, later runs that day
        # exit immediately without doing any work. Set this to just after
        # Bing rotates the image in your market's timezone (for en-US that is
        # midnight US Eastern). Only relevant when the script is run
        # repeatedly (login/scheduler); a manual run with --force ignores it.
        "check_start_hour": 7,
    },

    # macOS only: System Events enumerates desktop Spaces grouped by display.
    # On most Macs the MAIN display's Spaces come LAST in that list
    # (false = default). Set to true if your main display gets the oldest
    # wallpaper instead of today's.
    "main_display_first": False,

    "overlay": {
        "enabled": True,
        # "bottom-right", "bottom-left", "top-right", "top-left"
        "position": "bottom-right",
        # Distance from the image edge, in pixels (tuned for 4K).
        "margin": 48,

        # Extra clearance so the overlay never hides under the OS taskbar /
        # dock. "auto" detects it (real taskbar size on Windows, sensible
        # defaults on macOS/Linux). A number of pixels overrides it, and the
        # named presets cover common setups:
        #   "none"    -> 0    (no taskbar on the overlay's edge)
        #   "small"   -> 60   (slim taskbar / auto-hide)
        #   "medium"  -> 100  (default Windows taskbar at 4K w/ scaling)
        #   "large"   -> 160  (thick taskbar or dock)
        "taskbar_offset": "auto",
        # Inner padding of the info box.
        "padding": 28,
        # Rounded corner radius of the info box (0 = square corners).
        "corner_radius": 20,
        # Box color and opacity (0 = transparent, 255 = solid).
        "background_color": [0, 0, 0],
        "background_opacity": 150,
        "text_color": [255, 255, 255],
        # Font sizes in pixels (tuned for 4K; halve them for 1080p).
        "title_font_size": 48,
        "text_font_size": 32,
        # Show/hide the two text parts.
        "show_title": True,
        "show_copyright": True,
        # Show/hide just the author/copyright holder, e.g. the "(c) John Doe"
        # part of the details line. The location/description stays.
        "show_author": True,
        # Maximum width of the info box as a fraction of the image width.
        "max_width_ratio": 0.35,
        # Extra pixels between text lines.
        "line_spacing": 10,
    },
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict:
    if not path.exists():
        log.info("No config found, writing defaults to %s", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    with path.open("r", encoding="utf-8") as fh:
        user_cfg = json.load(fh)

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in user_cfg.items():
        if key == "$schema":
            continue  # editor hint, not an option
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


# --------------------------------------------------------------------------- #
# Bing API
# --------------------------------------------------------------------------- #

def _ssl_context() -> ssl.SSLContext:
    """Default context, preferring certifi's CA bundle when available.

    This fixes CERTIFICATE_VERIFY_FAILED on python.org macOS builds where
    "Install Certificates.command" was never run.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http_get(url: str, binary: bool = False, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
                return resp.read() if binary else resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise RuntimeError(
                    f"SSL certificate verification failed for {url}. "
                    "Install certifi (pip install certifi) or, on macOS with a "
                    "python.org build, run 'Install Certificates.command' from "
                    "the Python folder in Applications.") from exc
            wait = 2 ** attempt
            log.warning("Request failed (%s), retrying in %ds...", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def fetch_image_metadata(market: str, count: int) -> list[dict]:
    """Return metadata for `count` daily images, newest first."""
    images: list[dict] = []
    while len(images) < count:
        n = min(MAX_PER_REQUEST, count - len(images))
        url = BING_API.format(idx=len(images), n=n, mkt=market)
        data = json.loads(http_get(url))
        batch = data.get("images", [])
        if not batch:
            break
        images.extend(batch)
    if not images:
        raise RuntimeError(f"Bing returned no images for market '{market}'")
    return images


def image_url(urlbase: str, resolution: str) -> str:
    return f"{BING_BASE}{urlbase}_{resolution}.jpg"


def download_image(meta: dict, cfg: dict, dest_dir: Path) -> Path:
    """Download one image, trying the configured resolution with fallbacks."""
    urlbase = meta["urlbase"]
    start = meta.get("startdate", "00000000")  # e.g. "20261107"
    date_str = f"{start[0:4]}-{start[4:6]}-{start[6:8]}" if len(start) == 8 else start
    title = sanitize_filename(meta.get("title") or urlbase.split("OHR.")[-1] or "bing")
    dest = dest_dir / f"{date_str}_{cfg['market']}_{title}.jpg"
    if dest.exists():
        log.info("Already have %s", dest.name)
        return dest

    tried = []
    for res in dict.fromkeys([cfg["resolution"], "UHD", "1920x1080"]):
        url = image_url(urlbase, res)
        tried.append(res)
        try:
            blob = http_get(url, binary=True)
        except RuntimeError:
            continue
        if len(blob) < 20_000 or not blob.startswith(b"\xff\xd8"):
            log.warning("Unexpected content from %s, trying next resolution", url)
            continue
        dest.write_bytes(blob)
        log.info("Downloaded %s (%s, %.1f MB)", dest.name, res, len(blob) / 1e6)
        return dest
    raise RuntimeError(f"Could not download {urlbase} at any resolution (tried {tried})")


def sanitize_filename(name: str, limit: int = 60) -> str:
    name = re.sub(r"[^\w\- ]", "", name, flags=re.UNICODE).strip().replace(" ", "_")
    return (name or "wallpaper")[:limit]


# --------------------------------------------------------------------------- #
# Overlay
# --------------------------------------------------------------------------- #

FONT_CANDIDATES = {
    "Darwin": [
        ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc"),
        ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/SFNS.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ],
    "Windows": [
        ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ],
    "Linux": [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ],
}


def load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    idx = 1 if bold else 0
    for pair in FONT_CANDIDATES.get(platform.system(), []) + FONT_CANDIDATES["Linux"]:
        path = pair[idx]
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def wrap_text(text: str, font, draw, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


TASKBAR_PRESETS = {"none": 0, "small": 60, "medium": 100, "large": 160}


def taskbar_offset(ov: dict) -> int:
    """Extra bottom/side clearance so the overlay clears the OS taskbar/dock."""
    setting = ov.get("taskbar_offset", "auto")
    if isinstance(setting, (int, float)):
        return int(setting)
    if isinstance(setting, str) and setting in TASKBAR_PRESETS:
        return TASKBAR_PRESETS[setting]
    # "auto"
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes
            # SPI_GETWORKAREA gives the screen minus the taskbar; the
            # difference from the full screen height is the taskbar size.
            SPI_GETWORKAREA = 0x0030

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            work = RECT()
            ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                                       ctypes.byref(work), 0)
            screen_h = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
            used = screen_h - (work.bottom - work.top)
            return max(used, 0)
        except Exception:  # noqa: BLE001
            return TASKBAR_PRESETS["medium"]
    if system == "Darwin":
        return TASKBAR_PRESETS["medium"]  # Dock (usually auto-hidden or ~100px)
    return TASKBAR_PRESETS["medium"]      # Linux panels vary; medium is safe


def _overlay_hash(ov: dict) -> str:
    """A stable hash of the overlay settings that affect rendering."""
    import hashlib
    relevant = {k: ov.get(k) for k in (
        "position", "margin", "taskbar_offset", "padding", "corner_radius",
        "background_color", "background_opacity", "text_color",
        "title_font_size", "text_font_size", "show_title", "show_copyright",
        "show_author", "max_width_ratio", "line_spacing")}
    blob = json.dumps(relevant, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _overlay_path(clean: Path, ov: dict) -> Path:
    """Overlay filename for an image, encoding the rendering settings.

    The settings hash is part of the filename so that any config change
    produces a NEW path. macOS and Windows cache desktop pictures by file
    path, so re-rendering to the same path would leave the old image on
    screen; a new path forces the OS to reload it.
    """
    return clean.with_name(f"{clean.stem}_overlay_{_overlay_hash(ov)[:12]}.jpg")


def _purge_superseded_overlays(clean: Path, keep: Path) -> None:
    """Delete older overlay renders/sidecars for the same source image."""
    for old in clean.parent.glob(clean.stem + "_overlay*"):
        if old != keep and old.suffix in (".jpg", ".overlayhash"):
            try:
                old.unlink()
            except OSError:
                pass


def strip_author(copyright_text: str) -> str:
    """Remove the author/copyright holder from Bing's details line.

    Bing's "copyright" field looks like:
        "Bamboo forest, Pipiwai Trail, Maui, Hawaii (c) MNStudio/Getty Images)"
    The author is the trailing parenthesised part (usually containing "(c)").
    Returns the location/description with that part removed.
    """
    # Remove a trailing "(...)" group, typically the copyright holder.
    stripped = re.sub(r"\s*\([^()]*\)\s*$", "", copyright_text).strip()
    # Only strip if we actually removed something and text remains.
    return stripped if stripped else copyright_text


def apply_overlay(image_path: Path, out_path: Path, title: str, copyright_text: str,
                  ov: dict) -> Path:
    from PIL import Image, ImageDraw

    img = Image.open(image_path).convert("RGB")
    width, height = img.size
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    title_font = load_font(ov["title_font_size"], bold=True)
    text_font = load_font(ov["text_font_size"])
    max_text_w = int(width * ov["max_width_ratio"])

    lines: list[tuple[str, object]] = []
    if ov["show_title"] and title:
        lines.append((title, title_font))
    if ov["show_copyright"] and copyright_text:
        details = copyright_text if ov.get("show_author", True) else strip_author(copyright_text)
        lines.extend((ln, text_font) for ln in wrap_text(details, text_font, draw, max_text_w))
    if not lines:
        img.save(out_path, "JPEG", quality=95)
        return out_path

    padding, spacing = ov["padding"], ov["line_spacing"]
    line_sizes = []
    for text, font in lines:
        bbox = draw.textbbox((0, 0), text, font=font)
        line_sizes.append((bbox[2] - bbox[0], bbox[3] - bbox[1]))
    box_w = max(w for w, _ in line_sizes) + 2 * padding
    box_h = sum(h for _, h in line_sizes) + spacing * (len(lines) - 1) + 2 * padding

    margin = ov["margin"]
    pos = ov["position"]
    # Push the box away from the taskbar/dock edge. The taskbar is almost
    # always at the bottom, so only bottom positions get the extra offset.
    offset = taskbar_offset(ov) if "bottom" in pos else 0
    x0 = margin if "left" in pos else width - margin - box_w
    y0 = margin if "top" in pos else height - margin - offset - box_h

    bg = (*ov["background_color"], ov["background_opacity"])
    draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h],
                           radius=ov["corner_radius"], fill=bg)

    fg = (*ov["text_color"], 255)
    y = y0 + padding
    for (text, font), (_, line_h) in zip(lines, line_sizes):
        draw.text((x0 + padding, y), text, font=font, fill=fg)
        y += line_h + spacing

    out = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    out.save(out_path, "JPEG", quality=95)
    log.info("Overlay written to %s", out_path.name)
    return out_path


# --------------------------------------------------------------------------- #
# Monitor detection
# --------------------------------------------------------------------------- #

def monitor_count() -> int:
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes
            return max(ctypes.windll.user32.GetSystemMetrics(80), 1)  # SM_CMONITORS
        if system == "Darwin":
            out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                 capture_output=True, text=True, timeout=30).stdout
            return max(len(re.findall(r"Resolution:", out)), 1)
        # Linux / other X11
        if shutil.which("xrandr"):
            out = subprocess.run(["xrandr", "--listmonitors"],
                                 capture_output=True, text=True, timeout=15).stdout
            m = re.search(r"Monitors:\s*(\d+)", out)
            if m:
                return max(int(m.group(1)), 1)
            out = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=15).stdout
            return max(len(re.findall(r"^.* connected", out, re.M)), 1)
    except Exception as exc:  # noqa: BLE001 - detection is best-effort
        log.warning("Could not detect monitor count (%s), assuming 1", exc)
    return 1


# --------------------------------------------------------------------------- #
# Wallpaper setters (paths: one per monitor, index 0 = main monitor)
# --------------------------------------------------------------------------- #

def set_wallpapers(paths: list[Path], cfg: dict) -> None:
    system = platform.system()
    if system == "Darwin":
        set_wallpapers_macos(paths, main_first=bool(cfg.get("main_display_first", False)))
    elif system == "Windows":
        set_wallpapers_windows(paths)
    elif system == "Linux":
        set_wallpapers_linux(paths)
    else:
        raise RuntimeError(f"Unsupported OS: {system}")


def _macos_displays() -> list[dict]:
    """Physical displays in hardware order: name, resolution, main flag.

    Parsed from `system_profiler SPDisplaysDataType`, whose display order
    matches Quartz CGGetActiveDisplayList.
    """
    out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                         capture_output=True, text=True, timeout=60).stdout
    displays: list[dict] = []
    current: dict | None = None
    for line in out.splitlines():
        # A display name line: 8 spaces indent, ends with ':', not a key/value
        m = re.match(r"^ {8}(\S[^:]*):\s*$", line)
        if m and "Resolution" not in line:
            current = {"name": m.group(1).strip(), "main": False,
                       "w": 0, "h": 0}
            displays.append(current)
            continue
        if current is None:
            continue
        rm = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", line)
        if rm:
            current["w"], current["h"] = int(rm.group(1)), int(rm.group(2))
        if "Main Display: Yes" in line:
            current["main"] = True
    return [d for d in displays if d["w"]]


def _macos_desktop_display_names() -> list[str]:
    """`display name` of every System Events desktop (Space), in order."""
    count = int(subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get count of desktops'],
        capture_output=True, text=True, check=True).stdout.strip())
    names = []
    for i in range(1, count + 1):
        name = subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to get display name of desktop {i}'],
            capture_output=True, text=True, check=True).stdout.strip()
        names.append(name)
    return names


def set_wallpapers_macos(paths: list[Path], main_first: bool = False) -> None:
    """Set one wallpaper per physical display, main display first.

    "desktop N" in System Events is a *Space*, not a display, and its order
    does not reliably put the main display first (fullscreen apps also add
    extra Spaces). So we resolve each desktop Space to its physical display
    via the `display name` property, rank displays main-first, and assign
    paths[0] (today) to the main display's Space, paths[1] to the next, etc.
    """
    displays = _macos_displays()
    desktop_names = _macos_desktop_display_names()

    # Rank displays: main first, then hardware order. `main_first` config
    # flips to hardware order for setups where that matches better.
    ranked = sorted(range(len(displays)),
                    key=lambda i: (not displays[i]["main"], i))
    if main_first:
        ranked = list(range(len(displays)))

    # Map each System Events desktop index -> display rank, by display name.
    # `display name` looks like "VG255 (1)", "VG255 (2)", "ASUS VG289":
    # the base name matches system_profiler, the "(n)" suffix disambiguates
    # identical monitors (1-based, in hardware order).
    by_name: dict[str, list[int]] = {}
    for rank, di in enumerate(ranked):
        by_name.setdefault(displays[di]["name"], []).append(rank)

    def base_name(display_name: str) -> str:
        return re.sub(r"\s*\(\d+\)\s*$", "", display_name).strip()

    def occurrence(display_name: str) -> int:
        m = re.search(r"\((\d+)\)\s*$", display_name)
        return int(m.group(1)) - 1 if m else 0

    assignments: list[tuple[int, Path]] = []  # (desktop index, image)
    for desktop_idx, dname in enumerate(desktop_names, start=1):
        ranks = by_name.get(base_name(dname))
        if not ranks:
            continue
        rank = ranks[min(occurrence(dname), len(ranks) - 1)]
        assignments.append((desktop_idx, paths[min(rank, len(paths) - 1)]))
        log.info("macOS: desktop %d (%s) -> display rank %d -> %s",
                 desktop_idx, dname, rank,
                 paths[min(rank, len(paths) - 1)].name)

    if not assignments:  # fallback: simple 1:1 by index
        log.warning("Could not resolve displays by name; falling back to "
                    "index order")
        assignments = [(i, paths[min(i - 1, len(paths) - 1)])
                       for i in range(1, len(desktop_names) + 1)]

    def esc(p: Path) -> str:
        return str(p).replace("\\", "\\\\").replace('"', '\\"')

    for desktop_idx, path in assignments:
        script = (f'tell application "System Events" to set picture of '
                  f'desktop {desktop_idx} to (POSIX file "{esc(path)}")')
        subprocess.run(["osascript", "-e", script], check=True)


def set_wallpapers_windows(paths: list[Path]) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        import comtypes
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

        class IDesktopWallpaper(IUnknown):
            _iid_ = GUID("{B92B56A9-8B55-4E14-9A89-0199BBB6F93B}")
            _methods_ = [
                COMMETHOD([], HRESULT, "SetWallpaper",
                          (["in"], ctypes.c_wchar_p, "monitorID"),
                          (["in"], ctypes.c_wchar_p, "wallpaper")),
                COMMETHOD([], HRESULT, "GetWallpaper",
                          (["in"], ctypes.c_wchar_p, "monitorID"),
                          (["out"], ctypes.POINTER(ctypes.c_wchar_p), "wallpaper")),
                COMMETHOD([], HRESULT, "GetMonitorDevicePathAt",
                          (["in"], wintypes.UINT, "monitorIndex"),
                          (["out"], ctypes.POINTER(ctypes.c_wchar_p), "monitorID")),
                COMMETHOD([], HRESULT, "GetMonitorDevicePathCount",
                          (["out"], ctypes.POINTER(wintypes.UINT), "count")),
                COMMETHOD([], HRESULT, "GetMonitorRECT",
                          (["in"], ctypes.c_wchar_p, "monitorID"),
                          (["out"], ctypes.POINTER(wintypes.RECT), "monitorRect")),
                COMMETHOD([], HRESULT, "SetBackgroundColor",
                          (["in"], ctypes.c_ulong, "backgroundColor")),
                COMMETHOD([], HRESULT, "GetBackgroundColor",
                          (["out"], ctypes.POINTER(ctypes.c_ulong), "backgroundColor")),
                COMMETHOD([], HRESULT, "SetPosition",
                          (["in"], ctypes.c_int, "position")),
            ]

        dw = comtypes.CoCreateInstance("{C2CF3110-460E-4FC1-B9D0-8A1C0C9CC4BD}",
                                       interface=IDesktopWallpaper)
        count = dw.GetMonitorDevicePathCount()
        for i in range(count):
            monitor_id = dw.GetMonitorDevicePathAt(i)
            path = paths[min(i, len(paths) - 1)]
            dw.SetWallpaper(monitor_id, str(path))
            log.info("Windows: monitor %d -> %s", i, path.name)
        dw.SetPosition(4)  # DWPOS_FILL
    except ImportError:
        log.warning("comtypes not installed (pip install comtypes); "
                    "setting today's image on ALL monitors")
        import ctypes
        ctypes.windll.user32.SystemParametersInfoW(20, 0, str(paths[0]), 3)  # SPI_SETDESKWALLPAPER


def set_wallpapers_linux(paths: list[Path]) -> None:
    de = (os.environ.get("XDG_CURRENT_DESKTOP")
          or os.environ.get("DESKTOP_SESSION") or "").lower()
    if "kde" in de or "plasma" in de:
        return set_wallpapers_kde(paths)
    if "xfce" in de:
        return set_wallpapers_xfce(paths)
    if "cinnamon" in de or "x-cinnamon" in de:
        return set_wallpapers_gsettings(paths[0], "org.cinnamon.desktop.background")
    if any(x in de for x in ("gnome", "unity", "budgie", "pantheon")):
        return set_wallpapers_gsettings(paths[0], "org.gnome.desktop.background")
    # Fallback heuristics when the DE cannot be detected
    if shutil.which("qdbus6") or shutil.which("qdbus") or shutil.which("qdbus-qt6"):
        return set_wallpapers_kde(paths)
    if shutil.which("xfconf-query"):
        return set_wallpapers_xfce(paths)
    if shutil.which("gsettings"):
        return set_wallpapers_gsettings(paths[0], "org.gnome.desktop.background")
    if shutil.which("feh"):
        subprocess.run(["feh", "--bg-fill", str(paths[0])], check=True)
        log.info("feh: all monitors -> %s", paths[0].name)
        return
    raise RuntimeError("No supported way to set the wallpaper found "
                       "(tried KDE/qdbus, XFCE/xfconf, GNOME/gsettings, feh)")


def set_wallpapers_gsettings(path: Path, schema: str) -> None:
    uri = f"file://{path}"
    for key in ("picture-uri", "picture-uri-dark"):
        subprocess.run(["gsettings", "set", schema, key, uri], check=False)
    log.info("gsettings (%s): all monitors -> %s "
             "(per-monitor is not supported by this desktop)", schema, path.name)


def set_wallpapers_kde(paths: list[Path]) -> None:
    qdbus = next((shutil.which(n) for n in
                  ("qdbus6", "qdbus-qt6", "qdbus", "qdbus-qt5") if shutil.which(n)), None)
    if not qdbus:
        raise RuntimeError("qdbus not found; cannot set KDE wallpaper")
    uris = json.dumps([f"file://{p}" for p in paths])
    script = f"""
var imgs = {uris};
var ds = desktops();
for (var i = 0; i < ds.length; i++) {{
    var d = ds[i];
    d.wallpaperPlugin = "org.kde.image";
    d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
    d.writeConfig("Image", imgs[Math.min(i, imgs.length - 1)]);
}}
"""
    subprocess.run([qdbus, "org.kde.plasmashell", "/PlasmaShell",
                    "org.kde.PlasmaShell.evaluateScript", script], check=True)
    log.info("KDE Plasma: assigned %d image(s) across desktops", len(paths))


def set_wallpapers_xfce(paths: list[Path]) -> None:
    out = subprocess.run(["xfconf-query", "-c", "xfce4-desktop", "-l"],
                         capture_output=True, text=True, check=True).stdout
    bases = sorted({m.group(1) for line in out.splitlines()
                    if (m := re.match(r"(/backdrop/screen\d+/monitor.*/)(?:workspace\d+/)?last-image", line))})
    if not bases:
        raise RuntimeError("xfconf-query returned no monitor backdrop properties")
    for i, base in enumerate(bases):
        path = str(paths[min(i, len(paths) - 1)])
        for key in ("image-path", "last-image"):
            subprocess.run(["xfconf-query", "-c", "xfce4-desktop",
                            "-p", base + key, "-s", path], check=False)
        log.info("XFCE: %s -> %s", base, Path(path).name)


# --------------------------------------------------------------------------- #
# Run state (so repeat runs can skip once today's wallpaper is set)
# --------------------------------------------------------------------------- #

def _state_file(dest_dir: Path) -> Path:
    return dest_dir / ".state.json"


def _read_state(dest_dir: Path) -> dict:
    try:
        return json.loads(_state_file(dest_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(dest_dir: Path, **fields) -> None:
    state = _read_state(dest_dir)
    state.update(fields)
    try:
        _state_file(dest_dir).write_text(json.dumps(state, indent=2),
                                         encoding="utf-8")
    except OSError:
        pass


def _today_done_today(dest_dir: Path) -> bool:
    """True if today's wallpaper was already downloaded AND set today."""
    return _read_state(dest_dir).get("last_set_date") == time.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

def cleanup_old_files(dest_dir: Path, keep_days: int) -> None:
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    for pattern in ("*.jpg", "*.overlayhash"):
        for f in dest_dir.glob(pattern):
            if f.stat().st_mtime < cutoff:
                log.info("Removing old file %s", f.name)
                f.unlink()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Set Bing daily wallpapers per monitor (today on main, "
                    "yesterday on the second, etc.) with an info overlay.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                   help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--days", type=int, default=None,
                   help="How many daily images to fetch (overrides config 'days')")
    p.add_argument("--download-only", action="store_true",
                   help="Download and render images but do not change the wallpaper")
    p.add_argument("--force", action="store_true",
                   help="Force re-render of overlays even when settings are unchanged")
    p.add_argument("--quiet", action="store_true", help="Only log warnings/errors")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    cfg = load_config(args.config)
    dest_dir = Path(os.path.expanduser(cfg["download_dir"]))
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Skip early if today's wallpaper is already set and we're past the hour
    # from which new images are expected. Before that hour we always run (the
    # "window" in which the new image appears). --force bypasses the skip.
    start_hour = int(cfg.get("schedule", {}).get("check_start_hour", 7))
    now = time.localtime()
    if not args.force and now.tm_hour >= start_hour and _today_done_today(dest_dir):
        log.info("Today's wallpaper already set; nothing to do "
                 "(use --force to override).")
        return 0

    monitors = monitor_count()
    log.info("Detected %d monitor(s)", monitors)

    if args.days is not None:
        days = args.days
    elif cfg["days"] == "auto":
        days = monitors
    else:
        days = int(cfg["days"])
    days = max(days, 1)

    offsets = cfg["day_offsets"]
    if offsets:
        days = max(days, max(offsets) + 1)

    log.info("Fetching metadata for %d day(s) from market '%s'", days, cfg["market"])
    metadata = fetch_image_metadata(cfg["market"], days)

    # Download + overlay
    final_images: list[Path] = []
    overlay_cfg = cfg["overlay"]
    overlay_on = bool(overlay_cfg.get("enabled"))
    if overlay_on:
        try:
            import PIL  # noqa: F401
        except ImportError:
            log.warning("Pillow not installed (pip install Pillow); overlay disabled")
            overlay_on = False

    for meta in metadata:
        clean = download_image(meta, cfg, dest_dir)
        if not overlay_on:
            final_images.append(clean)
            continue
        out = _overlay_path(clean, overlay_cfg)
        # The settings hash is embedded in the filename, so a config change
        # yields a path that doesn't exist yet and triggers a re-render.
        if args.force or not out.exists():
            apply_overlay(clean, out, meta.get("title", ""),
                          meta.get("copyright", ""), overlay_cfg)
            _purge_superseded_overlays(clean, out)
        final_images.append(out)

    # Map monitors -> day offsets
    if offsets:
        monitor_images = [final_images[min(offsets[i] if i < len(offsets) else i,
                                           len(final_images) - 1)]
                          for i in range(monitors)]
    else:
        monitor_images = [final_images[min(i, len(final_images) - 1)]
                          for i in range(monitors)]

    if args.download_only or not cfg["set_wallpaper"]:
        log.info("Skipping wallpaper change (download-only)")
    else:
        set_wallpapers(monitor_images, cfg)
        _write_state(dest_dir, last_set_date=time.strftime("%Y-%m-%d"))

    cleanup_old_files(dest_dir, int(cfg["keep_days"]))
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
