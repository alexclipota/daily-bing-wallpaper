# Bing Daily Wallpaper (multi-monitor, cross-platform)

Downloads the Bing daily wallpapers in 4K and sets them across your monitors:

- **Main monitor** → today's wallpaper
- **Second monitor** → yesterday's wallpaper
- **Third monitor** → the wallpaper from 2 days ago, and so on

Each image gets a small overlay in a corner showing the wallpaper's **title and
details** (location, photographer, etc. — as provided by Bing).

Works on **macOS**, **Windows**, and **Linux** (KDE Plasma, XFCE, GNOME,
Cinnamon, or anything with `feh`).

## Setup

```bash
pip install -r requirements.txt
```

- `Pillow` is required (draws the overlay).
- `comtypes` is only needed on **Windows** for true per-monitor wallpapers
  (without it, today's image is set on all monitors).

## Usage

```bash
python3 bing_wallpaper.py                 # fetch + set wallpapers
python3 bing_wallpaper.py --download-only # fetch only, don't change wallpaper
python3 bing_wallpaper.py --days 4        # fetch 4 days regardless of monitors
python3 bing_wallpaper.py --config /path/to/config.json
```

On the first run a `config.json` is created next to the script. Images are
stored in `~/.bing-wallpapers` (configurable). Clean originals are kept
(`2026-11-07_en-US_Title.jpg`) alongside the overlaid versions
(`..._overlay_<hash>.jpg`) that are actually set as wallpaper.

### Applying config changes

Just **edit `config.json`, save it, and run the script again** — that's it:

```bash
python3 bing_wallpaper.py
```

The overlay filename contains a hash of all rendering settings
(`..._overlay_<hash>.jpg`). When you change any overlay option, the hash
changes, so the script renders a **new file** and sets that as the wallpaper.
This matters because macOS and Windows cache desktop pictures by file path —
re-rendering to the same path would leave the old image on screen.

Notes:
- **Overlay / appearance changes** (position, fonts, `show_author`, …) →
  re-render from the cached originals, no re-download.
- **`market` or `resolution` changes** → new images are downloaded under new
  filenames; old ones are cleaned up per `keep_days`.
- To force a re-render without editing the config (e.g. after resizing the
  Windows taskbar with `taskbar_offset: "auto"`), run
  `python3 bing_wallpaper.py --force`.

## Configuration (`config.json`)

`config.json` contains only the actual options. All descriptions and allowed
values live in **`config.schema.json`** (standard JSON Schema). The generated
`config.json` references it via `"$schema": "./config.schema.json"`, so
editors like VS Code give you **hover tooltips, autocomplete, and validation**
while editing the config — no plugins needed.

| Option | Default | Description |
|---|---|---|
| `market` | `"en-US"` | Bing market/locale to pull wallpapers from, e.g. `"en-GB"`, `"de-DE"`, `"fr-FR"`, `"ja-JP"`, `"ro-RO"`. Different markets sometimes get different images. |
| `resolution` | `"UHD"` | Download size. `"UHD"` = 3840x2160 (4K). Also: `"1920x1080"`, `"1920x1200"`, `"1366x768"`, `"1280x720"`, `"1080x1920"` (portrait)... Falls back to UHD, then 1080p if unavailable. |
| `download_dir` | `"~/.bing-wallpapers"` | Where images are stored. |
| `days` | `"auto"` | How many daily images to fetch. `"auto"` = one per detected monitor. |
| `day_offsets` | `null` | Explicit monitor → day-offset mapping (0 = today). E.g. `[0, 0, 3]` = today on monitors 1–2, 3-days-ago on monitor 3. `null` = `[0, 1, 2, ...]`. |
| `keep_days` | `14` | Delete images older than this many days (`0` = keep forever). |
| `set_wallpaper` | `true` | `false` = download only. |
| `schedule.check_start_hour` | `7` | Hour (0–23, local) from which the script looks for today's new wallpaper. Before it, every run checks; once today's wallpaper is set, later runs that day exit immediately. Set to just after Bing rotates in your market's timezone (en-US = midnight US Eastern). `--force` ignores it. |
| `main_display_first` | `false` | **Escape hatch — rarely needed.** macOS already gives the main display today's wallpaper by matching display names. Set `true` only if the main display still gets the wrong image (switches to plain hardware order). Ignored on Windows/Linux. |
| `overlay.enabled` | `true` | Draw the info box. |
| `overlay.position` | `"bottom-right"` | `"bottom-right"`, `"bottom-left"`, `"top-right"`, `"top-left"`. |
| `overlay.margin` | `48` | Distance from the image edge (px). |
| `overlay.taskbar_offset` | `"auto"` | Extra clearance so the overlay never hides under the taskbar/dock (bottom positions only). `"auto"` detects the real taskbar size on Windows, safe default elsewhere. Presets: `"none"` (0), `"small"` (60), `"medium"` (100), `"large"` (160), or any pixel number. |
| `overlay.padding` | `28` | Inner padding of the info box (px). |
| `overlay.corner_radius` | `20` | Rounded-corner radius of the box (px). |
| `overlay.background_color` | `[0,0,0]` | Box color (RGB). |
| `overlay.background_opacity` | `150` | 0 = transparent … 255 = solid. |
| `overlay.text_color` | `[255,255,255]` | Text color (RGB). |
| `overlay.title_font_size` | `48` | Title font size in px (tuned for 4K; halve for 1080p). |
| `overlay.text_font_size` | `32` | Details font size in px. |
| `overlay.show_title` / `overlay.show_copyright` | `true` | Toggle the title line / the details line. |
| `overlay.show_author` | `true` | Show the author/copyright holder (the trailing `(© Name)` part of the details line). `false` keeps the location/description but hides the author. |
| `overlay.max_width_ratio` | `0.35` | Max box width as a fraction of image width. |
| `overlay.line_spacing` | `10` | Extra px between lines. |

Changing the config re-renders the overlays on the next run.

## Run it automatically every day

The script is **idempotent and cheap to re-run**: once today's wallpaper is
downloaded and set, any further run that day exits immediately (see
`schedule.check_start_hour`). So it's safe to trigger it often — at login, on
a schedule, or both — and it will only do real work when there's a new image.

### macOS (launchd)

Create `~/Library/LaunchAgents/com.bingwallpaper.daily.plist` (adjust the
`python3` path — run `which python3` — and the script path):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bingwallpaper.daily</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/bing-wallpaper/bing_wallpaper.py</string>
  </array>

  <!-- Run shortly after Bing rotates the image, plus catch-ups. -->
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>15</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  </array>

  <!-- Also run at login, in case the Mac was asleep/off at the times above. -->
  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/tmp/bing-wallpaper.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/bing-wallpaper.log</string>
</dict>
</plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/com.bingwallpaper.daily.plist
```

`RunAtLoad` fires at every **login** (and right after `launchctl load`), so
waking/logging in triggers a check without waiting for the next scheduled
time. Because of the built-in skip, a login run after today's wallpaper is
already set costs nothing.

> The first run may prompt for permission to control "System Events"
> (System Settings → Privacy & Security → Automation).

### Windows (Task Scheduler)

```powershell
schtasks /create /tn "BingWallpaper" /sc daily /st 09:00 ^
  /tr "\"C:\Path\To\pythonw.exe\" \"C:\path\to\bing-wallpaper\bing_wallpaper.py\""
```

### Linux (cron)

```cron
0 9 * * * /usr/bin/python3 /path/to/bing-wallpaper/bing_wallpaper.py >> /tmp/bing-wallpaper.log 2>&1
```

> On GNOME/KDE, `gsettings`/`qdbus` need your session's D-Bus. If cron can't
> reach it, use a systemd user timer or run the script at login instead.

## Notes & limitations

- **macOS accuracy**: the script resolves each desktop *Space* to its
  physical display by reading the `display name` of every Space (System
  Events) and matching it against `system_profiler` display data (name +
  resolution + main-display flag). Today's image always lands on the **main**
  display, yesterday's on the next, etc. — correct even when apps are
  fullscreen (which create extra Spaces) or displays are rearranged. If a
  name can't be matched it falls back to index order.
- **Monitor order** follows the OS's display enumeration (usually the primary
  display first). If the order doesn't match your physical layout, use
  `day_offsets` to remap.
- **GNOME / Cinnamon / Budgie** don't support per-monitor wallpapers; today's
  image is set on all monitors there. KDE Plasma, XFCE, macOS and Windows get
  the full per-monitor treatment.
- The Bing API serves up to 8 images per request; the script pages
  automatically for more than 8 monitors/days.
