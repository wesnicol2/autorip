#!/usr/bin/env python3
"""
autorip.py - Automatic Blu-ray/DVD ripper for Unraid/MakeMKV

Usage:
  python3 autorip.py              # One-shot: rip disc if present, then exit
  python3 autorip.py --daemon     # Poll continuously (every POLL_INTERVAL_SECS)
  python3 autorip.py --post-process <staging_dir>
                                  # Name/move an already-ripped directory

Naming convention:
  Main feature: {Title} ({Year}) {edition-Blu-ray} [Automated MakeMKV Blu-ray Rip].mkv
  Extras:       {Extra Title} ({Year}) [Automated MakeMKV Blu-ray Rip].mkv
  Dest:         Movies/{Title} ({Year})/MakeMKV Rips/
  Extras dest:  Movies/{Title} ({Year})/MakeMKV Rips/Extras/
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

    # Rotating: 100 MB x 10 files = 1 GB max
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


# ─── Device ────────────────────────────────────────────────────────────────────

def ensure_device():
    """Recreate /dev/sr0 if missing (Unraid has no udev)."""
    if not os.path.exists(config.DEVICE):
        log.debug("Creating device node %s", config.DEVICE)
        subprocess.run(["mknod", config.DEVICE, "b", "11", "0"], check=True)
        subprocess.run(["chmod", "666", config.DEVICE], check=True)


def disc_is_present():
    """Return True if optical media is readable."""
    try:
        r = subprocess.run(
            ["dd", f"if={config.DEVICE}", "bs=2048", "count=1", "of=/dev/null"],
            capture_output=True,
            timeout=15,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ─── MakeMKV ───────────────────────────────────────────────────────────────────

def _mkv(*args, timeout=7200):
    cmd = ["docker", "exec", config.MAKEMKV_CONTAINER, "makemkvcon"] + list(args)
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def setup_makemkv_key():
    subprocess.run(
        ["docker", "exec", config.MAKEMKV_CONTAINER, "cp",
         "/config/settings.conf", "/root/.MakeMKV/settings.conf"],
        check=True, capture_output=True,
    )
    log.debug("MakeMKV key settings synced to container")


def get_disc_info():
    """
    Parse makemkvcon -r info output.
    Returns dict: {disc_type, disc_title, titles: [{index, duration_secs, size_mb, filename}]}
    """
    r = _mkv("-r", "info", "disc:0", timeout=120)
    if r.returncode != 0:
        log.error("makemkvcon info failed:\n%s", r.stderr)
        return None

    disc_type = "Unknown"
    disc_title = "Unknown"
    titles: dict[int, dict] = {}

    for line in r.stdout.splitlines():
        m = re.match(r'^CINFO:1,\d+,"(.+)"$', line)
        if m:
            disc_type = m.group(1)

        m = re.match(r'^CINFO:2,\d+,"(.+)"$', line)
        if m:
            disc_title = m.group(1)

        # Duration: TINFO:N,9,0,"HH:MM:SS"
        m = re.match(r'^TINFO:(\d+),9,\d+,"(\d+):(\d+):(\d+)"$', line)
        if m:
            idx = int(m.group(1))
            secs = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
            titles.setdefault(idx, {})["duration_secs"] = secs

        # Size: TINFO:N,11,0,"1,234 MB"
        m = re.match(r'^TINFO:(\d+),11,\d+,"([\d,\.]+) MB"$', line)
        if m:
            idx = int(m.group(1))
            titles.setdefault(idx, {})["size_mb"] = float(m.group(2).replace(",", ""))

        # Filename: TINFO:N,27,0,"name.mkv"
        m = re.match(r'^TINFO:(\d+),27,\d+,"(.+)"$', line)
        if m:
            idx = int(m.group(1))
            titles.setdefault(idx, {})["filename"] = m.group(2)

    titles_list = [{"index": k, **v} for k, v in sorted(titles.items())]
    return {"disc_type": disc_type, "disc_title": disc_title, "titles": titles_list}


def rip_disc(container_subdir: str):
    """
    Rip all titles to /output/{container_subdir} inside the container.
    That maps to {STAGING_BASE_DIR}/{container_subdir} on the host.
    """
    log.info("Starting MakeMKV rip to /output/%s ...", container_subdir)
    r = _mkv("mkv", "disc:0", "all", f"/output/{container_subdir}", timeout=7200)
    if r.returncode != 0:
        log.error("Rip failed:\n%s", r.stderr)
        return False
    log.info("Rip finished successfully")
    return True


# ─── TMDB ──────────────────────────────────────────────────────────────────────

_TMDB_HEADERS = None


def _tmdb_headers():
    global _TMDB_HEADERS
    if _TMDB_HEADERS is None:
        _TMDB_HEADERS = {"Authorization": f"Bearer {config.TMDB_READ_TOKEN}"}
    return _TMDB_HEADERS


def tmdb_search(title: str, year: int = None):
    """
    Search TMDB and return best match with runtime.
    Returns dict: {id, title, year, runtime_secs, runtime_min} or None.
    """
    params: dict = {"query": title, "include_adult": "false"}
    if year:
        params["year"] = year

    r = requests.get(
        "https://api.themoviedb.org/3/search/movie",
        headers=_tmdb_headers(), params=params, timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return None

    # Try to confirm by year if multiple results
    best = results[0]
    if year and len(results) > 1:
        for res in results:
            rel = (res.get("release_date") or "")[:4]
            if rel == str(year):
                best = res
                break

    detail = requests.get(
        f"https://api.themoviedb.org/3/movie/{best['id']}",
        headers=_tmdb_headers(), timeout=30,
    )
    detail.raise_for_status()
    data = detail.json()

    yr = None
    if data.get("release_date"):
        yr = int(data["release_date"][:4])

    rt_min = data.get("runtime") or 0
    return {
        "id": data["id"],
        "title": data["title"],
        "year": yr,
        "runtime_min": rt_min,
        "runtime_secs": rt_min * 60,
    }


def tmdb_search_with_runtime_confirm(title: str, disc_runtime_secs: int):
    """
    Search TMDB and confirm match by comparing runtime (within 2 min).
    Falls back to first result if no runtime match found.
    """
    r = requests.get(
        "https://api.themoviedb.org/3/search/movie",
        headers=_tmdb_headers(),
        params={"query": title, "include_adult": "false"},
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return None

    candidates = []
    for res in results[:5]:
        detail = requests.get(
            f"https://api.themoviedb.org/3/movie/{res['id']}",
            headers=_tmdb_headers(), timeout=30,
        )
        if detail.status_code != 200:
            continue
        data = detail.json()
        rt_min = data.get("runtime") or 0
        yr = int(data["release_date"][:4]) if data.get("release_date") else None
        candidates.append({
            "id": data["id"],
            "title": data["title"],
            "year": yr,
            "runtime_min": rt_min,
            "runtime_secs": rt_min * 60,
        })

    if not candidates:
        return None

    # Pick closest runtime match (within 2 minutes)
    tolerance = 120
    for c in candidates:
        if abs(c["runtime_secs"] - disc_runtime_secs) <= tolerance:
            return c

    return candidates[0]


# ─── DVDCompare ────────────────────────────────────────────────────────────────

def dvdcompare_search(title: str) -> list[tuple[str, str]]:
    """
    Search DVDCompare. Returns list of (fid, disc_label) tuples.
    """
    try:
        r = requests.post(
            "https://www.dvdcompare.net/comparisons/search.php",
            data={"param": title},
            headers={"User-Agent": "Mozilla/5.0 (compatible)"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("DVDCompare search failed: %s", e)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for a in soup.find_all("a", href=re.compile(r"/comparisons/film\.php\?fid=\d+")):
        m = re.search(r"fid=(\d+)", a["href"])
        if m:
            results.append((m.group(1), a.get_text(strip=True)))
    return results


def dvdcompare_get_extras(fid: str) -> list[dict]:
    """
    Fetch a DVDCompare disc page and extract extras with runtimes.
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
    seen_titles = set()

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        for i, cell in enumerate(cells):
            t_match = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", cell.get_text())
            if not t_match:
                continue
            h, m, s = map(int, t_match.groups())
            runtime_secs = h * 3600 + m * 60 + s
            if runtime_secs == 0:
                continue
            # Try leftmost non-empty cell as title
            title = ""
            for c in cells:
                txt = c.get_text(strip=True)
                if txt and not re.match(r"^\d+:\d{2}:\d{2}$", txt):
                    title = txt
                    break
            if title and title not in seen_titles:
                seen_titles.add(title)
                extras.append({"title": title, "runtime_secs": runtime_secs})
            break

    return extras


def match_extras_by_runtime(
    disc_titles: list[dict],
    dvdcompare_extras: list[dict],
    tolerance_secs: int = 5,
) -> dict[int, str]:
    """
    Match disc titles against DVDCompare extras by runtime within tolerance.
    Returns {disc_title_index: extra_name} only for confident matches.
    """
    matches: dict[int, str] = {}
    used: set[str] = set()

    for title in disc_titles:
        disc_rt = title.get("duration_secs", 0)
        if disc_rt == 0:
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
            log.debug(
                "  Matched t%02d (%ds) -> '%s' (diff=%ds)",
                title["index"], disc_rt, best["title"], best_diff,
            )
        else:
            log.debug(
                "  No DVDCompare match for t%02d (%ds), best diff=%ds",
                title["index"], disc_rt, int(best_diff) if best else -1,
            )

    return matches


# ─── Naming ────────────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return re.sub(r"\s+", " ", name).strip()


def _media_label(disc_type: str) -> str:
    return "Blu-ray" if "blu" in disc_type.lower() else "DVD"


def main_feature_filename(movie_title: str, year: int, disc_type: str) -> str:
    label = _media_label(disc_type)
    return f"{_safe(movie_title)} ({year}) {{edition-{label}}} [Automated MakeMKV {label} Rip].mkv"


def extra_filename(extra_title: str, year: int, disc_type: str) -> str:
    label = _media_label(disc_type)
    return f"{_safe(extra_title)} ({year}) [Automated MakeMKV {label} Rip].mkv"


# ─── Discord ───────────────────────────────────────────────────────────────────

def post_discord(message: str):
    if not getattr(config, "DISCORD_WEBHOOK_URL", ""):
        return
    try:
        r = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("Discord notification failed: %s", e)


# ─── Git ───────────────────────────────────────────────────────────────────────

def git_push(movie_title: str, year: int):
    repo = config.AUTORIP_DIR
    try:
        subprocess.run(["git", "-C", repo, "add", "-A"],
                       check=True, capture_output=True)
        msg = f"rip: {movie_title} ({year}) — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        result = subprocess.run(
            ["git", "-C", repo, "commit", "-m", msg],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(["git", "-C", repo, "push"],
                           check=True, capture_output=True)
            log.debug("Committed and pushed to GitHub")
        else:
            log.debug("Nothing new to commit to GitHub")
    except subprocess.CalledProcessError as e:
        log.warning("Git push failed: %s", e)


# ─── Pipeline ──────────────────────────────────────────────────────────────────

def process_disc():
    log.info("=" * 60)
    log.info("Disc detected — starting pipeline")

    setup_makemkv_key()

    # 1. Read disc info
    log.info("Reading disc info...")
    info = get_disc_info()
    if not info:
        log.error("Could not read disc info. Aborting.")
        return False

    disc_type = info["disc_type"]
    disc_title = info["disc_title"]
    titles = info["titles"]
    log.info("Disc type: %s | Title: %s | %d title(s)", disc_type, disc_title, len(titles))

    if not titles:
        log.error("No titles found. Aborting.")
        return False

    # 2. Identify long titles (potential main features / cuts)
    long_titles = [t for t in titles if t.get("duration_secs", 0) >= 3600]
    short_titles = [t for t in titles if t not in long_titles]

    if not long_titles:
        # Fall back to longest title
        longest = max(titles, key=lambda t: t.get("duration_secs", 0))
        long_titles = [longest]
        short_titles = [t for t in titles if t is not longest]

    # 3. TMDB lookup — use runtime of longest title to confirm match
    longest_rt = max(t.get("duration_secs", 0) for t in long_titles)
    log.info("Searching TMDB for '%s' (longest title: %dm)...", disc_title, longest_rt // 60)
    movie = tmdb_search_with_runtime_confirm(disc_title, longest_rt)

    if not movie:
        log.warning("TMDB lookup failed; using disc title and current year")
        movie_title = disc_title
        movie_year = datetime.now().year
        tmdb_runtime_secs = longest_rt
    else:
        movie_title = movie["title"]
        movie_year = movie["year"] or datetime.now().year
        tmdb_runtime_secs = movie["runtime_secs"]
        log.info("TMDB match: %s (%d) — %d min", movie_title, movie_year, movie["runtime_min"])

    # 4. Resolve main feature among long titles
    main_title = min(long_titles, key=lambda t: abs(t.get("duration_secs", 0) - tmdb_runtime_secs))
    cut_titles = [t for t in long_titles if t is not main_title]

    log.info(
        "Main feature: t%02d (%dm) | Alternate cuts: %d | Short titles: %d",
        main_title["index"],
        main_title.get("duration_secs", 0) // 60,
        len(cut_titles),
        len(short_titles),
    )

    # 5. Set up staging directory
    safe_slug = re.sub(r"[^a-z0-9_-]", "_", disc_title.lower())[:40]
    staging_dir = Path(config.STAGING_BASE_DIR) / safe_slug
    staging_dir.mkdir(parents=True, exist_ok=True)

    # 6. Rip
    if not rip_disc(safe_slug):
        log.error("Rip failed. Aborting.")
        return False

    # Hand off to post-processing
    return _post_process(staging_dir, movie_title, movie_year, disc_type,
                         main_title, cut_titles, short_titles)


def _post_process(
    staging_dir: Path,
    movie_title: str,
    movie_year: int,
    disc_type: str,
    main_title: dict,
    cut_titles: list,
    short_titles: list,
):
    """Name, move files, notify. Reusable for --post-process mode."""
    titles = [main_title] + cut_titles + short_titles
    all_indices = {t["index"] for t in titles}

    # 7. DVDCompare extras lookup
    log.info("Looking up extras on DVDCompare for '%s'...", movie_title)
    dvdcompare_extras: list[dict] = []
    fids = dvdcompare_search(movie_title)
    if fids:
        fid, label = fids[0]
        log.info("DVDCompare match: '%s' (fid=%s)", label, fid)
        dvdcompare_extras = dvdcompare_get_extras(fid)
        log.info("DVDCompare: found %d extras", len(dvdcompare_extras))
    else:
        log.warning("No DVDCompare results for '%s'; extras get generic names", movie_title)

    extra_name_map = match_extras_by_runtime(short_titles, dvdcompare_extras)

    # 8. Build destination dirs
    dest_base = Path(config.MOVIES_DIR) / f"{_safe(movie_title)} ({movie_year})" / "MakeMKV Rips"
    extras_dir = dest_base / "Extras"
    dest_base.mkdir(parents=True, exist_ok=True)
    extras_dir.mkdir(parents=True, exist_ok=True)
    log.info("Destination: %s", dest_base)

    # 9. Move and rename
    ripped = sorted(staging_dir.glob("*.mkv"))
    moved: list[str] = []

    for f in ripped:
        # Identify which title this file belongs to
        title_obj = None
        # Match by filename field from disc info
        for t in titles:
            if t.get("filename", "") == f.name:
                title_obj = t
                break
        # Fall back to index from filename suffix (_t00.mkv)
        if title_obj is None:
            m = re.search(r"_t(\d+)\.mkv$", f.name, re.IGNORECASE)
            if m:
                idx = int(m.group(1))
                title_obj = next((t for t in titles if t["index"] == idx), None)

        if title_obj is None:
            log.warning("Cannot identify title for %s — skipping", f.name)
            continue

        idx = title_obj["index"]

        if title_obj is main_title:
            new_name = main_feature_filename(movie_title, movie_year, disc_type)
            dest = dest_base / new_name
        elif title_obj in cut_titles:
            # Alternate cut: longer = Extended, shorter = Director's Cut
            diff_secs = title_obj.get("duration_secs", 0) - main_title.get("duration_secs", 0)
            cut_label = "Extended Cut" if diff_secs > 120 else "Director's Cut"
            new_name = main_feature_filename(
                f"{movie_title} - {cut_label}", movie_year, disc_type
            )
            dest = dest_base / new_name
        elif idx in extra_name_map:
            new_name = extra_filename(extra_name_map[idx], movie_year, disc_type)
            dest = extras_dir / new_name
        else:
            rt_min = title_obj.get("duration_secs", 0) // 60
            new_name = extra_filename(f"Bonus - {rt_min}min", movie_year, disc_type)
            dest = extras_dir / new_name

        shutil.move(str(f), str(dest))
        log.info("  %s  ->  %s", f.name, dest.relative_to(Path(config.MOVIES_DIR)))
        moved.append(str(dest))

    # 10. Eject
    try:
        subprocess.run(
            ["docker", "exec", config.MAKEMKV_CONTAINER, "eject", config.DEVICE],
            timeout=15, capture_output=True,
        )
        log.info("Disc ejected")
    except Exception as e:
        log.warning("Eject failed: %s", e)

    # 11. Cleanup empty staging dir
    if not list(staging_dir.glob("*")):
        staging_dir.rmdir()

    # 12. Discord notification
    label = _media_label(disc_type)
    n_extras = len(moved) - 1 - len(cut_titles)
    msg_parts = [f"**Disc ripped:** {movie_title} ({movie_year}) [{label}]"]
    if cut_titles:
        msg_parts.append(f"Alternate cuts: {len(cut_titles)}")
    if n_extras > 0:
        msg_parts.append(f"Extras: {n_extras}")
    msg_parts.append(f"Location: Movies/{_safe(movie_title)} ({movie_year})/MakeMKV Rips/")
    post_discord("\n".join(msg_parts))

    # 13. Git
    git_push(movie_title, movie_year)

    log.info("Pipeline complete: %s (%d) — %d files moved", movie_title, movie_year, len(moved))
    return True


def post_process_mode(staging_dir_path: str):
    """
    --post-process mode: identify and move files in an already-ripped directory.
    Asks the user for movie title/year since we can't read the disc.
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

    # Build title objects from filenames
    titles = []
    for f in ripped:
        m = re.search(r"_t(\d+)\.mkv$", f.name, re.IGNORECASE)
        idx = int(m.group(1)) if m else len(titles)
        # Try to get duration via ffprobe if available
        duration_secs = _probe_duration(f)
        titles.append({"index": idx, "filename": f.name, "duration_secs": duration_secs})

    titles.sort(key=lambda t: t["index"])

    # Prompt for movie identity
    print("\nFiles found:")
    for t in titles:
        print(f"  t{t['index']:02d}: {t['filename']} ({t['duration_secs']//60}min)")

    movie_title = input("\nMovie title (as on TMDB): ").strip()
    movie_year_str = input("Year: ").strip()
    disc_type_str = input("Disc type [Blu-ray/DVD]: ").strip() or "Blu-ray disc"
    movie_year = int(movie_year_str) if movie_year_str.isdigit() else datetime.now().year

    # Confirm on TMDB
    movie = tmdb_search(movie_title, movie_year)
    if movie:
        log.info("TMDB: %s (%d) — %d min", movie["title"], movie["year"], movie["runtime_min"])
        tmdb_rt = movie["runtime_secs"]
        movie_title = movie["title"]
        movie_year = movie["year"] or movie_year
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

    return _post_process(
        staging_dir, movie_title, movie_year, disc_type_str,
        main_title, cut_titles, short_titles,
    )


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


# ─── Entry point ───────────────────────────────────────────────────────────────

def check_and_run():
    ensure_device()
    if not disc_is_present():
        log.info("No disc present")
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
    parser.add_argument(
        "--daemon", action="store_true",
        help=f"Poll for disc every {config.POLL_INTERVAL_SECS}s",
    )
    parser.add_argument(
        "--interval", type=int, default=config.POLL_INTERVAL_SECS,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--post-process", metavar="STAGING_DIR",
        help="Name and move files from an already-ripped directory",
    )
    args = parser.parse_args()

    if args.post_process:
        post_process_mode(args.post_process)
    elif args.daemon:
        log.info("Daemon mode: polling every %ds", args.interval)
        while True:
            try:
                check_and_run()
            except Exception as e:
                log.exception("Unhandled error in pipeline: %s", e)
            time.sleep(args.interval)
    else:
        check_and_run()


if __name__ == "__main__":
    main()
