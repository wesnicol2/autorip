#!/usr/bin/env python3
"""
autorip.py - Automatic Blu-ray/DVD ripper for Unraid/MakeMKV

Usage:
  python3 autorip.py              # One-shot: rip disc if present, then exit
  python3 autorip.py --daemon     # Poll continuously (every POLL_INTERVAL_SECS)
  python3 autorip.py --post-process <staging_dir>
                                  # Name/move an already-ripped directory

Naming convention:
  Main feature: Title (Year) {edition-Blu-ray} [Automated MakeMKV Blu-ray Rip].mkv
  Extras:       Title (Year) - Extra Name-featurette.mkv
  Dest:         Movies/Title (Year)/MakeMKV Rips/
  Extras dest:  Movies/Title (Year)/MakeMKV Rips/Extras/
"""

import sys
import os

# Persistent deps installed to appdata so they survive container restarts
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

import argparse
import fcntl
import logging
import re
import shutil
import subprocess
import time
import unicodedata
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import config
except ImportError:
    print("ERROR: config.py not found. Copy config.py.example to config.py and fill in values.")
    sys.exit(1)


# ─── Logging ───────────────────────────────────────────────────────────────────

def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    log = logging.getLogger("autorip")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    fh = RotatingFileHandler(
        os.path.join(config.LOG_DIR, "autorip.log"),
        maxBytes=100 * 1024 * 1024,
        backupCount=10,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


log = setup_logging()


# ─── Device helpers ────────────────────────────────────────────────────────────

def _find_sg_device():
    """Return /dev/sgN path for the optical drive by scanning /sys (type 5 = optical)."""
    sg_base = "/sys/class/scsi_generic"
    if not os.path.exists(sg_base):
        return None
    for sg_name in sorted(os.listdir(sg_base)):
        type_file = os.path.join(sg_base, sg_name, "device", "type")
        try:
            with open(type_file) as f:
                if f.read().strip() == "5":
                    return f"/dev/{sg_name}"
        except (FileNotFoundError, IOError):
            pass
    return None


def ensure_device():
    """Recreate /dev/sr0 and the optical sg device if missing (Unraid has no udev)."""
    if not os.path.exists("/dev/sr0"):
        log.debug("Creating /dev/sr0")
        subprocess.run(["mknod", "/dev/sr0", "b", "11", "0"], check=True)
        subprocess.run(["chmod", "666", "/dev/sr0"], check=True)

    sg_dev = _find_sg_device()
    if sg_dev and not os.path.exists(sg_dev):
        minor = int(sg_dev.replace("/dev/sg", ""))
        log.debug("Creating %s", sg_dev)
        subprocess.run(["mknod", sg_dev, "c", "21", str(minor)], check=True)
        subprocess.run(["chmod", "666", sg_dev], check=True)


_COOLDOWN_FILE = "/tmp/autorip_cooldown"
_COOLDOWN_SECS = 300  # 5 min after a rip before re-triggering


def disc_is_present():
    """
    Return True if optical media is loaded.

    /sys/block/sr0/size returns a stale non-zero value after eject until the kernel
    gets a new media-change event.  We suppress false positives with a cooldown file
    written after each successful rip+eject.
    """
    # Cooldown: don't re-trigger for 5 min after a completed rip
    try:
        ts = float(Path(_COOLDOWN_FILE).read_text())
        if time.time() - ts < _COOLDOWN_SECS:
            log.debug("Post-rip cooldown active — skipping disc check")
            return False
        Path(_COOLDOWN_FILE).unlink(missing_ok=True)
    except (FileNotFoundError, ValueError):
        pass

    try:
        with open("/sys/block/sr0/size") as f:
            return int(f.read().strip()) > 0
    except (FileNotFoundError, ValueError):
        pass
    # Fallback: /proc/partitions
    try:
        with open("/proc/partitions") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 4 and parts[3] == "sr0":
                    return int(parts[2]) > 0
    except Exception:
        pass
    return False


# ─── MakeMKV ───────────────────────────────────────────────────────────────────

def _mkv(*args, timeout=7200):
    """
    Run makemkvcon in a dedicated ephemeral container.

    The MakeMKV GUI container's guiserver sends SIGTERM to any competing
    CLI process on the same drive, so we stop it first and use our own
    --rm container that mounts the appdata (containing the licence key)
    at /root/.MakeMKV where the CLI expects it.
    """
    sg_dev = _find_sg_device()
    cmd = [
        "docker", "run", "--rm",
        "--name", "makemkv-autorip",
        "-v", f"{config.MAKEMKV_APPDATA}:/root/.MakeMKV:rw",
        "-v", f"{config.STAGING_BASE_DIR}:/output:rw",
        "--device", "/dev/sr0:/dev/sr0",
    ]
    if sg_dev:
        cmd += ["--device", f"{sg_dev}:{sg_dev}"]
    cmd += [
        "--entrypoint", "/opt/makemkv/bin/makemkvcon",
        config.MAKEMKV_IMAGE,
    ] + list(args)
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def get_disc_info():
    """
    Parse makemkvcon -r info output.
    Returns dict: {disc_type, disc_title, titles: [{index, duration_secs, size_mb, filename}]}
    or None on failure.
    """
    r = _mkv("-r", "info", "disc:0", timeout=120)
    output = r.stdout + r.stderr
    if r.returncode != 0 and "title" not in output.lower():
        log.error("makemkvcon info failed:\n%s", output[-1000:])
        return None

    disc_type = "Unknown"
    disc_title = "Unknown"
    titles: dict[int, dict] = {}

    for line in output.splitlines():
        m = re.match(r'^CINFO:1,\d+,"(.+)"$', line)
        if m:
            disc_type = m.group(1)

        m = re.match(r'^CINFO:2,\d+,"(.+)"$', line)
        if m:
            disc_title = m.group(1)

        # Duration: TINFO:N,9,0,"H:MM:SS"
        m = re.match(r'^TINFO:(\d+),9,\d+,"(\d+):(\d+):(\d+)"$', line)
        if m:
            idx = int(m.group(1))
            secs = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
            titles.setdefault(idx, {"index": idx})["duration_secs"] = secs

        # Size in MB: TINFO:N,11,0,"1,234 MB"
        m = re.match(r'^TINFO:(\d+),11,\d+,"([\d,\.]+) MB"$', line)
        if m:
            idx = int(m.group(1))
            titles.setdefault(idx, {"index": idx})["size_mb"] = float(m.group(2).replace(",", ""))

        # Output filename: TINFO:N,27,0,"name.mkv"
        m = re.match(r'^TINFO:(\d+),27,\d+,"(.+)"$', line)
        if m:
            idx = int(m.group(1))
            titles.setdefault(idx, {"index": idx})["filename"] = m.group(2)

    titles_list = sorted(titles.values(), key=lambda t: t["index"])
    log.info("Disc type: %s | Name: %s | %d title(s)", disc_type, disc_title, len(titles_list))
    return {"disc_type": disc_type, "disc_title": disc_title, "titles": titles_list}


def rip_disc(container_subdir: str) -> bool:
    """Rip all titles to /output/{container_subdir} inside the container."""
    log.info("Starting MakeMKV rip to /output/%s ...", container_subdir)
    r = _mkv("mkv", "disc:0", "all", f"/output/{container_subdir}", timeout=7200)
    combined = r.stdout + r.stderr
    if r.returncode != 0:
        log.error("Rip failed (exit %d):\n%s", r.returncode, combined[-1000:])
        return False
    log.info("Rip finished successfully")
    return True


# ─── TMDB ──────────────────────────────────────────────────────────────────────

def _tmdb(path: str, **params):
    """Authenticated TMDB API GET."""
    params["api_key"] = config.TMDB_API_KEY
    r = requests.get(
        f"https://api.themoviedb.org/3/{path.lstrip('/')}",
        params=params, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def tmdb_search(title: str, year: int = None):
    """Search TMDB and return a single best match with runtime, or None."""
    params = {"query": title, "include_adult": "false"}
    if year:
        params["year"] = year
    results = _tmdb("search/movie", **params).get("results", [])
    if not results:
        return None

    best = results[0]
    if year and len(results) > 1:
        for res in results:
            if (res.get("release_date") or "")[:4] == str(year):
                best = res
                break

    data = _tmdb(f"movie/{best['id']}")
    yr = int(data["release_date"][:4]) if data.get("release_date") else None
    rt = data.get("runtime") or 0
    return {"id": data["id"], "title": data["title"], "year": yr,
            "runtime_min": rt, "runtime_secs": rt * 60}


def tmdb_search_with_runtime_confirm(title: str, disc_runtime_secs: int):
    """
    Search TMDB and confirm match by comparing runtime (within 2 min).
    Falls back to first result if no runtime match found.
    """
    results = _tmdb("search/movie", query=title, include_adult="false").get("results", [])
    if not results:
        return None

    candidates = []
    for res in results[:5]:
        try:
            data = _tmdb(f"movie/{res['id']}")
        except Exception:
            continue
        rt = data.get("runtime") or 0
        yr = int(data["release_date"][:4]) if data.get("release_date") else None
        candidates.append({"id": data["id"], "title": data["title"], "year": yr,
                           "runtime_min": rt, "runtime_secs": rt * 60})

    if not candidates:
        return None

    # Return closest runtime match within 2 minutes
    by_diff = sorted(candidates, key=lambda c: abs(c["runtime_secs"] - disc_runtime_secs))
    if abs(by_diff[0]["runtime_secs"] - disc_runtime_secs) <= 120:
        return by_diff[0]

    log.warning("No TMDB result within 120s tolerance; using first result")
    return candidates[0]


# ─── DVDCompare ────────────────────────────────────────────────────────────────

def dvdcompare_search(title: str) -> list[tuple[str, str]]:
    """Search DVDCompare. Returns list of (fid, disc_label) tuples."""
    try:
        r = requests.post(
            "https://www.dvdcompare.net/comparisons/search.php",
            data={"title": title, "submit": "Search"},
            headers={"User-Agent": "Mozilla/5.0 (compatible)"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("DVDCompare search failed: %s", e)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for a in soup.find_all("a", href=re.compile(r"fid=\d+")):
        m = re.search(r"fid=(\d+)", a["href"])
        if m:
            results.append((m.group(1), a.get_text(strip=True)))
    return results


def dvdcompare_get_extras(fid: str) -> list[dict]:
    """
    Fetch a DVDCompare film page and extract extras with runtimes.
    Returns list of {title, runtime_secs}.
    """
    try:
        r = requests.get(
            f"https://www.dvdcompare.net/comparisons/film.php?fid={fid}",
            headers={"User-Agent": "Mozilla/5.0 (compatible)"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("DVDCompare fetch failed for fid=%s: %s", fid, e)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    extras = []
    seen = set()

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        for cell in cells:
            text = cell.get_text()
            # Match MM:SS or HH:MM:SS
            t = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", text)
            if not t:
                continue
            a, b, c = t.groups()
            runtime_secs = (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))
            if runtime_secs < 60:
                continue
            # Use leftmost non-time cell as title
            title = ""
            for tc in cells:
                txt = tc.get_text(strip=True)
                if txt and not re.match(r"^\d+:\d{2}(:\d{2})?$", txt):
                    title = txt
                    break
            if title and title not in seen:
                seen.add(title)
                extras.append({"title": title, "runtime_secs": runtime_secs})
            break

    return extras


def match_extras_by_runtime(
    disc_titles: list[dict],
    dvdcompare_extras: list[dict],
    tolerance_secs: int = 5,
) -> dict[int, str]:
    """
    Match disc short titles against DVDCompare extras by runtime.
    Returns {disc_title_index: extra_name} for confident matches only.
    """
    matches: dict[int, str] = {}
    used: set[str] = set()

    for title in disc_titles:
        disc_rt = title.get("duration_secs", 0)
        if not disc_rt:
            continue
        best = None
        best_diff = float("inf")
        for extra in dvdcompare_extras:
            if extra["title"] in used:
                continue
            diff = abs(extra["runtime_secs"] - disc_rt)
            if diff < best_diff:
                best_diff = diff
                best = extra
        if best and best_diff <= tolerance_secs:
            matches[title["index"]] = best["title"]
            used.add(best["title"])
            log.debug("  Matched t%02d (%ds) -> '%s' (diff=%ds)",
                      title["index"], disc_rt, best["title"], best_diff)
        else:
            log.debug("  No DVDCompare match for t%02d (%ds), best diff=%ds",
                      title["index"], disc_rt, int(best_diff) if best else -1)

    return matches


# ─── Naming ────────────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    return re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)).strip()


def _media_label(disc_type: str) -> str:
    return "Blu-ray" if "blu" in disc_type.lower() else "DVD"


def main_feature_filename(movie_title: str, year: int, disc_type: str,
                           cut_label: str = None) -> str:
    media = _media_label(disc_type)
    edition = f"{media} {cut_label}" if cut_label else media
    return f"{_safe(movie_title)} ({year}) {{edition-{edition}}} [Automated MakeMKV {media} Rip].mkv"


def extra_filename(movie_title: str, year: int, extra_name: str) -> str:
    return f"{_safe(movie_title)} ({year}) - {_safe(extra_name)}-featurette.mkv"


# ─── Discord ───────────────────────────────────────────────────────────────────

def post_discord(message: str):
    url = getattr(config, "DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(url, json={"content": message}, timeout=10).raise_for_status()
    except Exception as e:
        log.warning("Discord notification failed: %s", e)


# ─── Pipeline ──────────────────────────────────────────────────────────────────

def process_disc():
    log.info("=" * 60)
    log.info("Disc detected — starting pipeline")

    # Stop GUI container — its guiserver SIGTERMs competing CLI processes
    log.info("Stopping %s GUI container...", config.MAKEMKV_CONTAINER)
    subprocess.run(["docker", "stop", config.MAKEMKV_CONTAINER],
                   capture_output=True, timeout=30)

    try:
        # 1. Read disc info
        log.info("Reading disc info...")
        info = get_disc_info()
        if not info:
            log.error("Could not read disc info. Aborting.")
            return False

        disc_type = info["disc_type"]
        disc_title = info["disc_title"]
        titles = info["titles"]
        if not titles:
            log.error("No titles found on disc. Aborting.")
            return False

        # 2. Identify long titles (≥60 min = main feature candidates)
        MAIN_MIN = 3600
        long_titles = [t for t in titles if t.get("duration_secs", 0) >= MAIN_MIN]
        short_titles = [t for t in titles if t not in long_titles
                        and t.get("duration_secs", 0) > 0]

        if not long_titles:
            longest = max(titles, key=lambda t: t.get("duration_secs", 0))
            long_titles = [longest]
            short_titles = [t for t in titles if t is not longest and t.get("duration_secs", 0) > 0]

        # 3. TMDB lookup
        longest_rt = max(t.get("duration_secs", 0) for t in long_titles)
        log.info("Searching TMDB for '%s' (longest title: %dm)...",
                 disc_title, longest_rt // 60)
        movie = None
        try:
            movie = tmdb_search_with_runtime_confirm(disc_title, longest_rt)
            if not movie:
                clean = re.sub(r"[_\-]", " ", disc_title).strip()
                movie = tmdb_search_with_runtime_confirm(clean, longest_rt)
        except Exception as e:
            log.warning("TMDB lookup failed: %s", e)

        if movie:
            movie_title = movie["title"]
            movie_year = movie["year"] or datetime.now().year
            tmdb_rt = movie["runtime_secs"]
            log.info("TMDB match: %s (%d) — %d min", movie_title, movie_year, movie["runtime_min"])
        else:
            movie_title = re.sub(r"[_\-]", " ", disc_title).title().strip()
            movie_year = datetime.now().year
            tmdb_rt = longest_rt
            log.warning("No TMDB match; using disc name '%s' and year %d", movie_title, movie_year)

        # 4. Resolve main feature among long titles
        main_title = min(long_titles, key=lambda t: abs(t.get("duration_secs", 0) - tmdb_rt))
        cut_titles = [t for t in long_titles if t is not main_title]

        log.info("Main feature: t%02d (%dm) | Alt cuts: %d | Extras: %d",
                 main_title["index"], main_title.get("duration_secs", 0) // 60,
                 len(cut_titles), len(short_titles))

        # 5. Set up staging directory
        safe_slug = re.sub(r"[^a-z0-9_-]", "_", disc_title.lower())[:40].strip("_")
        staging_dir = Path(config.STAGING_BASE_DIR) / safe_slug
        staging_dir.mkdir(parents=True, exist_ok=True)

        # 6. Rip
        if not rip_disc(safe_slug):
            log.error("Rip failed. Aborting.")
            return False

        return _post_process(
            staging_dir, movie_title, movie_year, disc_type,
            main_title, cut_titles, short_titles,
        )

    finally:
        log.info("Restarting %s GUI container...", config.MAKEMKV_CONTAINER)
        subprocess.run(["docker", "start", config.MAKEMKV_CONTAINER],
                       capture_output=True, timeout=30)


def _post_process(
    staging_dir: Path,
    movie_title: str,
    movie_year: int,
    disc_type: str,
    main_title: dict,
    cut_titles: list,
    short_titles: list,
):
    """Name, move files, eject, notify. Also callable from --post-process mode."""
    all_titles = [main_title] + cut_titles + short_titles
    warnings: list[str] = []

    # 7. DVDCompare extras lookup
    log.info("Looking up extras on DVDCompare for '%s'...", movie_title)
    dvdcompare_extras: list[dict] = []
    fids = []
    try:
        fids = dvdcompare_search(movie_title)
    except Exception as e:
        log.warning("DVDCompare lookup failed: %s", e)
        warnings.append(f"DVDCompare unavailable: {e}")

    if fids:
        fid, label = fids[0]
        log.info("DVDCompare match: '%s' (fid=%s)", label, fid)
        dvdcompare_extras = dvdcompare_get_extras(fid)
        log.info("DVDCompare: %d extras parsed", len(dvdcompare_extras))
    else:
        log.warning("No DVDCompare results for '%s'; extras get generic names", movie_title)
        warnings.append("DVDCompare: no match — extras named generically")

    extra_name_map = match_extras_by_runtime(short_titles, dvdcompare_extras)

    # 8. Build destination dirs
    folder = f"{_safe(movie_title)} ({movie_year})"
    dest_rips = Path(config.MOVIES_DIR) / folder / "MakeMKV Rips"
    dest_extras = dest_rips / "Extras"
    dest_rips.mkdir(parents=True, exist_ok=True)
    log.info("Destination: %s", dest_rips)

    # 9. Move and rename
    ripped = sorted(staging_dir.glob("*.mkv"))
    moved: list[str] = []
    n_extras_unmatched = 0

    for f in ripped:
        # Identify which title this file belongs to
        title_obj = None
        for t in all_titles:
            if t.get("filename", "") == f.name:
                title_obj = t
                break
        if title_obj is None:
            m = re.search(r"_t(\d+)\.mkv$", f.name, re.IGNORECASE)
            if m:
                idx = int(m.group(1))
                title_obj = next((t for t in all_titles if t["index"] == idx), None)

        if title_obj is None:
            log.warning("Cannot identify title for %s — skipping", f.name)
            continue

        idx = title_obj["index"]

        if title_obj is main_title:
            new_name = main_feature_filename(movie_title, movie_year, disc_type)
            dest = dest_rips / new_name

        elif title_obj in cut_titles:
            diff = title_obj.get("duration_secs", 0) - main_title.get("duration_secs", 0)
            cut_label = "Extended Cut" if diff > 120 else "Director's Cut"
            new_name = main_feature_filename(movie_title, movie_year, disc_type, cut_label)
            dest = dest_rips / new_name

        else:
            dest_extras.mkdir(parents=True, exist_ok=True)
            if idx in extra_name_map:
                new_name = extra_filename(movie_title, movie_year, extra_name_map[idx])
            else:
                n_extras_unmatched += 1
                new_name = extra_filename(movie_title, movie_year,
                                          f"Extra {n_extras_unmatched:02d}")
            dest = dest_extras / new_name

        shutil.move(str(f), str(dest))
        log.info("  %s  ->  %s", f.name, dest.relative_to(Path(config.MOVIES_DIR)))
        moved.append(str(dest))

    # 10. Write cooldown so cron doesn't re-trigger on stale sysfs size while disc is still in drive
    Path(_COOLDOWN_FILE).write_text(str(time.time()))
    log.info("Rip complete — disc left in drive, remove when ready")

    # 11. Cleanup empty staging dir
    remaining = list(staging_dir.glob("*"))
    if not remaining:
        staging_dir.rmdir()
    else:
        log.warning("%d file(s) left in staging dir (not moved): %s",
                    len(remaining), [f.name for f in remaining])

    # 12. Discord notification
    media = _media_label(disc_type)
    n_main = 1 + len(cut_titles)
    n_extras = len(moved) - n_main
    n_matched = n_extras - n_extras_unmatched
    parts = [f"**Disc ripped:** {movie_title} ({movie_year}) [{media}]"]
    if cut_titles:
        parts.append(f"Cuts: {1 + len(cut_titles)} (theatrical + {len(cut_titles)} alternate)")
    if n_extras:
        parts.append(f"Extras: {n_extras} ({n_matched} named, {n_extras_unmatched} unmatched)")
    for w in warnings:
        parts.append(f"⚠️ {w}")
    parts.append(f"📁 Movies/{folder}/MakeMKV Rips/")
    post_discord("\n".join(parts))

    log.info("Pipeline complete: %s (%d) — %d file(s) moved", movie_title, movie_year, len(moved))
    return True


# ─── Post-process mode ─────────────────────────────────────────────────────────

def _probe_duration(path: Path) -> int:
    """Use ffprobe to get duration in seconds, or 0 if unavailable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return int(float(r.stdout.strip()))
    except Exception:
        pass
    return 0


def post_process_mode(staging_dir_path: str):
    """
    --post-process mode: identify and move files in an already-ripped directory.
    Used when a disc was ripped manually and needs naming/moving.
    """
    staging_dir = Path(staging_dir_path)
    if not staging_dir.exists():
        log.error("Directory not found: %s", staging_dir)
        return False

    ripped = sorted(staging_dir.glob("*.mkv"))
    if not ripped:
        log.error("No .mkv files in %s", staging_dir)
        return False

    log.info("Post-process mode: %s (%d files)", staging_dir, len(ripped))

    titles = []
    for f in ripped:
        m = re.search(r"_t(\d+)\.mkv$", f.name, re.IGNORECASE)
        idx = int(m.group(1)) if m else len(titles)
        titles.append({"index": idx, "filename": f.name,
                       "duration_secs": _probe_duration(f)})
    titles.sort(key=lambda t: t["index"])

    print("\nFiles found:")
    for t in titles:
        print(f"  t{t['index']:02d}: {t['filename']} ({t['duration_secs']//60}min)")

    movie_title = input("\nMovie title (as on TMDB): ").strip()
    movie_year_str = input("Year: ").strip()
    disc_type_str = input("Disc type [Blu-ray/DVD]: ").strip() or "Blu-ray disc"
    movie_year = int(movie_year_str) if movie_year_str.isdigit() else datetime.now().year

    movie = None
    try:
        movie = tmdb_search(movie_title, movie_year)
    except Exception as e:
        log.warning("TMDB lookup failed: %s", e)

    if movie:
        log.info("TMDB: %s (%d) — %d min", movie["title"], movie["year"], movie["runtime_min"])
        movie_title = movie["title"]
        movie_year = movie["year"] or movie_year
        tmdb_rt = movie["runtime_secs"]
    else:
        log.warning("No TMDB result; using provided title/year")
        tmdb_rt = max((t["duration_secs"] for t in titles), default=0)

    long_titles = [t for t in titles if t["duration_secs"] >= 3600]
    short_titles = [t for t in titles if t not in long_titles]
    if not long_titles:
        long_titles = [max(titles, key=lambda t: t["duration_secs"])]
        short_titles = [t for t in titles if t not in long_titles]

    main_title = min(long_titles, key=lambda t: abs(t["duration_secs"] - tmdb_rt))
    cut_titles = [t for t in long_titles if t is not main_title]

    return _post_process(staging_dir, movie_title, movie_year, disc_type_str,
                         main_title, cut_titles, short_titles)


# ─── Entry point ───────────────────────────────────────────────────────────────

def check_and_run():
    ensure_device()
    if not disc_is_present():
        log.debug("No disc present")
        return

    lock_fd = open(config.LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Another rip is in progress (lock held) — skipping")
        lock_fd.close()
        return

    try:
        process_disc()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        try:
            os.remove(config.LOCK_FILE)
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Automatic Blu-ray/DVD ripper")
    parser.add_argument("--daemon", action="store_true",
                        help=f"Poll every {config.POLL_INTERVAL_SECS}s")
    parser.add_argument("--interval", type=int, default=config.POLL_INTERVAL_SECS)
    parser.add_argument("--post-process", metavar="STAGING_DIR",
                        help="Name and move files from an already-ripped directory")
    args = parser.parse_args()

    if args.post_process:
        post_process_mode(args.post_process)
    elif args.daemon:
        log.info("Daemon mode: polling every %ds", args.interval)
        while True:
            try:
                check_and_run()
            except Exception as e:
                log.exception("Unhandled error: %s", e)
            time.sleep(args.interval)
    else:
        check_and_run()


if __name__ == "__main__":
    main()
