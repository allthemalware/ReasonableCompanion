#!/usr/bin/env python3
"""Download Reason sound packs that the authenticated account can access.

The script deliberately uses only the Python standard library.  It supports
native Windows/macOS installs and Wine prefixes on Linux.
"""

import argparse
import base64
import errno
import getpass
import glob
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import PurePosixPath


__version__ = "1.0.2"

API_BASE = "https://nautilus.reasonstudios.com"
USER_AGENT = "ReasonCompanion/3.3.1"
CATALOG_PAGE_SIZE = 100
REQUEST_DELAY = 0.25
DOWNLOAD_TIMEOUT = 3600
TOKEN_EXPIRY_MARGIN = 30
# Current name first; the legacy Reason+ Companion name is the fallback.
COMPANION_NAMES = ("Reason Companion", "Reason+ Companion")
WINDOWS_INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
STAGING_PREFIX = ".reason-pack-download-"


# -- Path detection ----------------------------------------------------------

def _expand_path(path):
    return os.path.abspath(os.path.expanduser(path))


def _same_path(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def _newest_mtime(directory):
    """Return the newest modification time of a directory or its files."""
    newest = 0.0
    try:
        newest = os.path.getmtime(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return newest


def _wine_user_names(wine_prefix, wine_user=None):
    """Return Wine user directories, one per real profile, in name order.

    Users that are symbolic links to another profile (for example
    ``bob -> alice``) are the same profile and are listed only once.
    """
    users_dir = os.path.join(wine_prefix, "drive_c", "users")
    if not os.path.isdir(users_dir):
        return []

    excluded = {"all users", "default", "default user", "public"}
    users = [
        name for name in os.listdir(users_dir)
        if os.path.isdir(os.path.join(users_dir, name)) and name.casefold() not in excluded
    ]
    users.sort(key=str.casefold)
    if wine_user:
        matching = [name for name in users if name.casefold() == wine_user.casefold()]
        if not matching:
            raise RuntimeError(
                "Wine user {!r} was not found in {}.".format(wine_user, users_dir)
            )
        return matching

    unique = []
    seen = set()
    # Prefer real directories over symlinks so the displayed name is the
    # profile's own folder.
    for name in sorted(users, key=lambda n: (os.path.islink(os.path.join(users_dir, n)), n.casefold())):
        real = os.path.realpath(os.path.join(users_dir, name))
        if real not in seen:
            seen.add(real)
            unique.append(name)
    unique.sort(key=str.casefold)
    return unique


def _profile(roaming_dir, label):
    """Describe one Windows profile's Packs directory and Companion session."""
    packs_dir = os.path.join(roaming_dir, "Propellerhead Software", "Packs")
    sessions = []
    for name_rank, name in enumerate(COMPANION_NAMES):
        path = os.path.join(roaming_dir, name, "Local Storage", "leveldb")
        if os.path.isdir(path):
            sessions.append((_newest_mtime(path), -name_rank, path))
    sessions.sort(reverse=True)
    return {
        "label": label,
        "packs_dir": packs_dir,
        "leveldb": sessions[0][2] if sessions else None,
        "session_mtime": sessions[0][0] if sessions else 0.0,
        "session_is_current": bool(sessions) and sessions[0][1] == 0,
        "has_packs": os.path.isdir(packs_dir),
    }


def _profile_rank(profile):
    # The most recently used Companion session identifies the account whose
    # packs should be installed; the current Companion name and an existing
    # Packs directory break ties.
    return (
        profile["leveldb"] is not None,
        profile["session_mtime"],
        profile["session_is_current"],
        profile["has_packs"],
    )


def wine_profiles(wine_prefix, wine_user=None):
    prefix = _expand_path(wine_prefix)
    profiles = []
    for user in _wine_user_names(prefix, wine_user):
        roaming = os.path.join(prefix, "drive_c", "users", user, "AppData", "Roaming")
        profiles.append(_profile(roaming, "{} (Wine user {})".format(prefix, user)))
    return profiles


def wine_prefix_candidates():
    """Return conventional Wine/Bottles prefixes without scanning arbitrary disks."""
    candidates = []
    env_prefix = os.environ.get("WINEPREFIX")
    if env_prefix:
        candidates.append(env_prefix)
    candidates.append("~/.wine")

    bottle_roots = (
        "~/.var/app/com.usebottles.bottles/data/bottles/bottles",
        "~/.local/share/bottles/bottles",
    )
    for root in bottle_roots:
        candidates.extend(sorted(glob.glob(os.path.join(os.path.expanduser(root), "*"))))

    unique = []
    seen = set()
    for candidate in candidates:
        candidate = _expand_path(candidate)
        if candidate not in seen and os.path.isdir(candidate):
            unique.append(candidate)
            seen.add(candidate)
    return unique


def find_local_install(wine_prefix=None, wine_user=None):
    """Choose one profile for both the Companion session and the Packs folder.

    Returns a dict with ``packs_dir`` and ``leveldb`` (either may be None) and
    a human-readable ``label``. Nothing is created or modified.
    """
    system = platform.system()
    if wine_prefix:
        prefixes = [wine_prefix]
    elif system == "Linux":
        prefixes = wine_prefix_candidates()
    else:
        prefixes = []

    profiles = []
    for prefix in prefixes:
        try:
            profiles.extend(wine_profiles(prefix, wine_user))
        except RuntimeError:
            # --wine-user without --wine-prefix: skip prefixes lacking that user.
            if wine_prefix:
                raise
    if wine_prefix and not profiles:
        raise RuntimeError(
            "No Windows user profiles were found in {}.".format(_expand_path(wine_prefix))
        )
    if not wine_prefix:
        # Without an explicit prefix only consider profiles that look like a
        # Reason installation.
        profiles = [p for p in profiles if p["leveldb"] or p["has_packs"]]

    if not profiles:
        if system == "Windows":
            appdata = os.environ.get("APPDATA") or os.path.join(
                os.path.expanduser("~"), "AppData", "Roaming"
            )
            profiles = [_profile(appdata, "Windows user profile")]
        elif system == "Darwin":
            profiles = [_profile(
                os.path.expanduser("~/Library/Application Support"), "macOS user profile"
            )]
        else:
            return {"label": None, "packs_dir": None, "leveldb": None}

    return max(profiles, key=_profile_rank)


# -- Authentication ----------------------------------------------------------

_SESSION_OBJECT_RE = re.compile(
    r'\{(?=[^{}]{0,8192}\})(?=[^{}]*"accessToken"\s*:\s*"(?P<access>[^"]+)")'
    r'(?=[^{}]*"refreshToken"\s*:\s*"(?P<refresh>[^"]+)")[^{}]*\}',
    re.DOTALL,
)


class CompanionSessionExpired(RuntimeError):
    """Companion's stored session can no longer be used without renewing it."""


class NotEnoughSpace(RuntimeError):
    """The disk cannot hold the next pack; continuing would only fail again."""


def token_expiry(token):
    """Return a JWT's ``exp`` claim without verifying it, or None."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        expiry = json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))).get("exp")
    except (ValueError, TypeError, AttributeError):
        return None
    return expiry if isinstance(expiry, (int, float)) else None


def _expired(token, margin=TOKEN_EXPIRY_MARGIN):
    expiry = token_expiry(token)
    return expiry is not None and expiry - margin <= time.time()


def discover_session_tokens(db_dir):
    """Return the newest access/refresh token pair stored by Companion.

    LevelDB keeps old and new values side by side in ``.log`` and ``.ldb``
    files, so the pair whose access token expires last is chosen; file order
    only breaks ties. The files are opened read-only.
    """
    files = []
    for pattern in ("*.log", "*.ldb"):
        files.extend(glob.glob(os.path.join(db_dir, pattern)))
    files = sorted(set(files), key=os.path.getmtime)

    best = None
    order = 0
    for file_path in files:
        try:
            with open(file_path, "rb") as handle:
                contents = handle.read().decode("latin1", errors="ignore")
        except OSError:
            continue
        for match in _SESSION_OBJECT_RE.finditer(contents):
            order += 1
            expiry = token_expiry(match.group("access"))
            key = (expiry if expiry is not None else float("-inf"), order)
            if best is None or key > best[0]:
                best = (key, match.group("access"), match.group("refresh"))

    if best is None:
        return None
    return best[1], best[2]


COMPANION_EXPIRED_HELP = (
    "Reason Companion's saved sign-in has expired. Companion renews it only "
    "while it is in use, and this script never renews Companion's session "
    "itself, so that Companion stays signed in. Click around in Companion "
    "(for example open the Packs page) and run this again within a few "
    "minutes, or use --login to sign in with a separate session."
)


class SessionAuth:
    """Supplies bearer tokens.

    Sessions created by this script (``--login`` or tokens passed on the
    command line) are renewed with the refresh token. A session borrowed from
    Companion's local storage is only ever read: renewing it could rotate the
    refresh token Companion relies on and sign Companion out.
    """

    def __init__(
        self, access_token=None, refresh_token=None, username=None, password=None,
        companion_db=None,
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.companion_db = companion_db
        self.borrowed = False
        self.last_refresh = 0

        if not self.access_token and not self.refresh_token:
            if username and password:
                self.login_with_credentials(username, password)
            else:
                self.borrowed = True
                self.load_companion_session()
        elif self.refresh_token and not self.access_token:
            self.refresh()

        if not self.access_token:
            raise RuntimeError("Authentication did not provide an access token.")

    def load_companion_session(self):
        if not self.companion_db:
            raise RuntimeError(
                "Could not locate Reason Companion session data. Open Companion "
                "and sign in first, use --login, or pass --wine-prefix."
            )
        tokens = discover_session_tokens(self.companion_db)
        if not tokens:
            raise RuntimeError(
                "No cached auth session found in Reason Companion. Open Companion "
                "and sign in first, or use --login."
            )
        changed = tokens[0] != self.access_token
        self.access_token = tokens[0]
        # The refresh token is deliberately not kept; see the class docstring.
        self.refresh_token = None
        if _expired(self.access_token):
            raise CompanionSessionExpired(COMPANION_EXPIRED_HELP)
        return changed

    def login_with_credentials(self, username, password):
        payload = json.dumps({"username": username, "password": password}).encode("utf-8")
        request = urllib.request.Request(
            API_BASE + "/auth/login", data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")
            raise RuntimeError("Login failed (HTTP {}): {}".format(error.code, message))
        except urllib.error.URLError as error:
            raise RuntimeError("Authentication request failed: {}".format(error.reason))

        self.access_token = data.get("accessToken")
        self.refresh_token = data.get("refreshToken")
        self.last_refresh = time.time()
        if not self.access_token:
            raise RuntimeError("Login succeeded but the response had no access token.")
        print("[auth] Logged in successfully as {}.".format(username))

    def refresh(self):
        if self.borrowed:
            raise RuntimeError("Refusing to renew Reason Companion's own session.")
        if not self.refresh_token:
            raise RuntimeError("Cannot refresh because no refresh token is available.")
        payload = json.dumps({"refreshToken": self.refresh_token}).encode("utf-8")
        request = urllib.request.Request(
            API_BASE + "/auth/refresh", data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")
            raise RuntimeError("Token refresh failed (HTTP {}): {}".format(error.code, message))
        except urllib.error.URLError as error:
            raise RuntimeError("Token refresh request failed: {}".format(error.reason))

        access_token = data.get("accessToken", self.access_token)
        if not access_token:
            raise RuntimeError("Token refresh response had no access token.")
        self.access_token = access_token
        self.refresh_token = data.get("refreshToken", self.refresh_token)
        self.last_refresh = time.time()

    def renew(self):
        """Obtain a newer access token after a 401. Returns True on success."""
        if self.borrowed:
            # Companion may have renewed its session in the meantime.
            return self.load_companion_session()
        if self.refresh_token:
            self.refresh()
            return True
        return False

    def get_token(self):
        if self.borrowed:
            if _expired(self.access_token):
                self.load_companion_session()
        elif self.refresh_token:
            expiry = token_expiry(self.access_token)
            if expiry is not None:
                stale = expiry - TOKEN_EXPIRY_MARGIN <= time.time()
            else:
                stale = time.time() - self.last_refresh > 240
            if stale:
                self.refresh()
        return self.access_token

    def headers(self):
        return {"Authorization": "Bearer {}".format(self.get_token()), "User-Agent": USER_AGENT}


# -- API operations ----------------------------------------------------------

def _api_url(endpoint):
    return "{}/{}".format(API_BASE, endpoint.lstrip("/"))


def api_get(endpoint, auth, timeout=20):
    """GET JSON from Nautilus, renewing authentication once after a 401."""
    for attempt in range(2):
        request = urllib.request.Request(_api_url(endpoint), headers=auth.headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 401 and attempt == 0 and auth.renew():
                continue
            if error.code == 401 and getattr(auth, "borrowed", False):
                raise CompanionSessionExpired(COMPANION_EXPIRED_HELP)
            raise RuntimeError("GET {} failed (HTTP {}).".format(endpoint, error.code))
        except urllib.error.URLError as error:
            raise RuntimeError("GET {} failed: {}".format(endpoint, error.reason))
    raise RuntimeError("GET {} failed after renewing authentication.".format(endpoint))


def api_post(endpoint, payload, auth, timeout=20):
    """POST JSON to Nautilus."""
    request = urllib.request.Request(
        _api_url(endpoint), data=json.dumps(payload).encode("utf-8"),
        headers=dict(auth.headers(), **{"Content-Type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError("POST {} failed (HTTP {}).".format(endpoint, error.code))
    except urllib.error.URLError as error:
        raise RuntimeError("POST {} failed: {}".format(endpoint, error.reason))


def fetch_pack_rsmeta(pack_id, auth):
    data = api_get("metadata/pack/?ids={}".format(pack_id), auth)
    if isinstance(data, list) and data:
        return data[0].get("metadata")
    return None


def fetch_download_url(pack_id, auth):
    data = api_get("presigned-url/{}/".format(pack_id), auth)
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        return data["url"]
    return None


def register_installed_pack(pack_id, auth):
    return api_post("packinstallations/", {"packId": pack_id}, auth)


def validate_pack_title(title):
    """Return a title that is safe as one folder name on every target OS."""
    if not isinstance(title, str) or not title or title in (".", ".."):
        raise ValueError("empty or relative pack title")
    if title != title.strip() or any(ord(char) < 32 for char in title):
        raise ValueError("pack title contains leading/trailing whitespace or a control character")
    if any(char in WINDOWS_INVALID_FILENAME_CHARS for char in title):
        raise ValueError("pack title contains a path separator or Windows-invalid character")
    return title


def _add_catalog_entry(packs, hit):
    """Validate one catalog entry and add it; return True if its ID is new."""
    if not isinstance(hit, dict) or not isinstance(hit.get("id"), int):
        raise RuntimeError("The pack catalog contains an entry without an integer ID.")
    try:
        title = validate_pack_title(hit.get("title"))
    except ValueError as error:
        raise RuntimeError(
            "Pack ID {} has an unsafe title: {}.".format(hit.get("id"), error)
        )
    is_new = hit["id"] not in packs
    packs[hit["id"]] = {"id": hit["id"], "title": title}
    return is_new


def list_released_packs(auth, page_size=CATALOG_PAGE_SIZE):
    """List every currently released pack.

    ``pack/`` returns the complete catalog as one list. Older service versions
    answered with a paginated search result instead; that form is still
    handled. (An empty search, ``q_text=``, now matches nothing, so it cannot
    be used to list the catalog.)
    """
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")

    data = api_get("pack/", auth)
    packs = {}
    if isinstance(data, list):
        for hit in data:
            _add_catalog_entry(packs, hit)
        if not packs:
            raise RuntimeError("The pack catalog is empty.")
        return [packs[pack_id] for pack_id in sorted(packs)]
    if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
        raise RuntimeError("The pack catalog returned an unexpected response.")

    expected_total = None
    page = 1
    while expected_total is None or len(packs) < expected_total:
        query = urllib.parse.urlencode({"q_page": page, "q_size": page_size})
        data = api_get("pack/?" + query, auth)
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            raise RuntimeError("The pack catalog returned an unexpected response.")
        total = data.get("totalMatches")
        if not isinstance(total, int) or total < 0:
            raise RuntimeError("The pack catalog did not provide a valid totalMatches value.")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("The pack catalog changed while it was being listed; please run again.")

        hits = data["hits"]
        new_ids = sum(_add_catalog_entry(packs, hit) for hit in hits)
        if not hits:
            if len(packs) < expected_total:
                raise RuntimeError("The pack catalog ended before all results were returned.")
            break
        if not new_ids and len(packs) < expected_total:
            raise RuntimeError(
                "The pack catalog returned page {} with no new packs; stopping "
                "instead of requesting it forever.".format(page)
            )
        page += 1

    if not packs:
        raise RuntimeError("The pack catalog is empty.")
    return [packs[pack_id] for pack_id in sorted(packs)]


# -- Filesystem and unpacking ------------------------------------------------

def get_installed_packs(packs_dir):
    """Return installed pack folder names, case-folded.

    Reason runs on case-insensitive file systems (including under Wine), so
    "Acid Techno" and "acid techno" are the same pack folder.
    """
    if not os.path.isdir(packs_dir):
        return set()
    return {
        name.casefold() for name in os.listdir(packs_dir)
        if os.path.isdir(os.path.join(packs_dir, name)) and not name.startswith(".")
    }


def _pack_folder_exists(packs_dir, title):
    if os.path.lexists(os.path.join(packs_dir, title)):
        return True
    try:
        names = os.listdir(packs_dir)
    except OSError:
        return False
    return title.casefold() in {name.casefold() for name in names}


def clean_archive_artifacts(directory):
    for root, directories, files in os.walk(directory, topdown=False):
        for filename in files:
            if filename == ".DS_Store":
                try:
                    os.unlink(os.path.join(root, filename))
                except OSError:
                    pass
        for dirname in directories:
            if dirname == "__MACOSX":
                shutil.rmtree(os.path.join(root, dirname), ignore_errors=True)


def _safe_member_path(name):
    """Map a zip member name to safe relative path parts.

    Path traversal is rejected. Characters that Windows cannot store in a file
    name are replaced with "-": packs made on macOS often contain ":" (how a
    "/" typed in Finder is stored), which Windows would otherwise turn into an
    alternate data stream.
    """
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("archive contains an unsafe path: {!r}".format(name))
    parts = []
    for part in path.parts:
        part = "".join(
            "-" if char in WINDOWS_INVALID_FILENAME_CHARS or ord(char) < 32 else char
            for char in part
        )
        if part in ("", ".", ".."):
            raise ValueError("archive contains an unsafe path: {!r}".format(name))
        parts.append(part)
    return parts


def extract_archive(archive_path, target_dir):
    """Extract a zip into ``target_dir`` exactly as laid out in the archive.

    Symbolic links and paths that would escape ``target_dir`` are rejected,
    and names that are invalid on Windows are made safe. Two members that
    would end up at the same path are an error rather than an overwrite.
    """
    root = os.path.realpath(target_dir)
    with zipfile.ZipFile(archive_path, "r") as archive:
        plan = []
        claimed = {}
        for info in archive.infolist():
            if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                raise ValueError("archive contains a symbolic link: {!r}".format(info.filename))
            parts = _safe_member_path(info.filename)
            if not parts:
                continue
            key = "/".join(parts).casefold()
            if claimed.setdefault(key, info.filename) != info.filename:
                raise ValueError("archive members {!r} and {!r} map to the same file".format(
                    claimed[key], info.filename,
                ))
            target = os.path.join(root, *parts)
            if os.path.commonpath([root, os.path.realpath(target)]) != root:
                raise ValueError("archive contains an unsafe path: {!r}".format(info.filename))
            plan.append((info, target))

        for info, target in plan:
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination, 1 << 20)
    clean_archive_artifacts(target_dir)


def check_free_space(directory, archive_size):
    """Require room for the archive, its unpacked contents and 1 GB spare."""
    needed = archive_size * 2 + (1 << 30)
    free = shutil.disk_usage(directory).free
    if free < needed:
        raise NotEnoughSpace(
            "Not enough free disk space in {}: the next pack needs about {} MB "
            "and {} MB are free.".format(directory, needed >> 20, free >> 20)
        )


def _download_to_file(download_url, destination, verbose):
    request = urllib.request.Request(download_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        try:
            total_size = int(response.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            total_size = 0
        check_free_space(os.path.dirname(destination), total_size)
        downloaded = 0
        last_percent = -1
        with open(destination, "wb") as handle:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                percent = downloaded * 100 // total_size if total_size else 0
                if verbose and total_size and percent != last_percent:
                    last_percent = percent
                    print(
                        "\r    {:3d}% [{}/{} MB]".format(
                            percent,
                            downloaded >> 20,
                            total_size >> 20,
                        ),
                        end="", flush=True,
                    )
    if verbose and total_size:
        print()


def staging_root(packs_dir):
    """Directory for temporary downloads: next to Packs, never inside it.

    Keeping work files out of the Packs folder means Reason and Companion
    never see a half-extracted pack, and keeping them on the same disk avoids
    filling a RAM-backed /tmp and makes the final move a rename.
    """
    return os.path.dirname(os.path.abspath(packs_dir))


def download_and_extract(pack_id, title, download_url, auth, packs_dir, sync_cloud=True, verbose=False):
    """Download, safely unpack, index, and optionally register one pack."""
    title = validate_pack_title(title)
    destination = os.path.join(packs_dir, title)
    if _pack_folder_exists(packs_dir, title):
        return "skipped"

    os.makedirs(packs_dir, exist_ok=True)
    moved_destination = False
    try:
        with tempfile.TemporaryDirectory(dir=staging_root(packs_dir), prefix=STAGING_PREFIX) as work_dir:
            archive_path = os.path.join(work_dir, "pack.zip")
            extract_dir = os.path.join(work_dir, "extracted")
            os.mkdir(extract_dir)
            _download_to_file(download_url, archive_path, verbose)
            extract_archive(archive_path, extract_dir)
            os.unlink(archive_path)

            # Keep the archive's own layout, as Companion does: a pack zip with
            # one top-level folder installs as Packs/<title>/<folder>/.
            extracted = extract_dir

            # Companion may have installed the same pack while this one was
            # downloading; never merge into or replace its folder.
            if _pack_folder_exists(packs_dir, title):
                return "skipped"
            try:
                os.rename(extracted, destination)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                shutil.move(extracted, destination)
            moved_destination = True

        try:
            rsmeta = fetch_pack_rsmeta(pack_id, auth)
            if rsmeta is not None:
                with open(os.path.join(destination, ".rsmeta"), "w", encoding="utf-8") as handle:
                    json.dump(rsmeta, handle, indent=2)
        except CompanionSessionExpired:
            raise
        except Exception as error:
            if verbose:
                print("  ! metadata indexing skipped: {}".format(error))

        if sync_cloud:
            try:
                register_installed_pack(pack_id, auth)
            except CompanionSessionExpired:
                raise
            except Exception as error:
                if verbose:
                    print("  ! cloud registration skipped: {}".format(error))
        return "installed"
    except NotEnoughSpace:
        raise
    except CompanionSessionExpired:
        # The pack itself is installed; only metadata/registration is missing.
        if not moved_destination:
            raise
        print("  ! installed, but metadata and cloud registration were skipped (session expired)")
        raise
    except Exception as error:
        if moved_destination and os.path.isdir(destination):
            shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeError(
            "Failed to install {} (ID {}): {}".format(title, pack_id, error)
        ) from error


# -- Command line ------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Download all currently released Reason packs available to your account.",
    )
    parser.add_argument("--start", type=int, help="Only include pack IDs at or above this value")
    parser.add_argument("--end", type=int, help="Only include pack IDs at or below this value")
    parser.add_argument("-o", "--output", help="Directory where sound packs will be installed")
    parser.add_argument("--wine-prefix", help="Wine/Bottles prefix containing Reason Companion")
    parser.add_argument("--wine-user", help="Wine user profile to use with --wine-prefix")
    parser.add_argument("--login", action="store_true", help="Prompt for Reason account credentials")
    parser.add_argument("-u", "--username", help="Reason account email or username")
    parser.add_argument("-p", "--password", help=argparse.SUPPRESS)
    parser.add_argument(
        "--password-stdin", action="store_true",
        help="Read the Reason account password from standard input",
    )
    parser.add_argument("--token", help="Reason access token")
    parser.add_argument("--refresh-token", help="Reason refresh token")
    parser.add_argument("--no-sync", action="store_true", help="Do not register downloads with Companion cloud sync")
    parser.add_argument("--dry-run", action="store_true", help="List available packs without creating or downloading anything")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show download progress and non-fatal warnings")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def _selected_packs(packs, start, end):
    return [
        pack for pack in packs
        if (start is None or pack["id"] >= start)
        and (end is None or pack["id"] <= end)
    ]


def _make_output_safe():
    """Never crash on characters the output encoding lacks.

    On Windows, output redirected to a file or pipe uses the ANSI code page,
    which has no "✓" or "→", and pack titles can contain any character.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _make_output_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.start is not None and args.start < 1:
        parser.error("--start must be a positive pack ID")
    if args.end is not None and args.end < 1:
        parser.error("--end must be a positive pack ID")
    if args.start is not None and args.end is not None and args.start > args.end:
        parser.error("--start cannot be greater than --end")
    if args.password and args.password_stdin:
        parser.error("use only one of --password and --password-stdin")
    if args.username and not (args.login or args.password or args.password_stdin):
        parser.error("--username requires --login, --password, or --password-stdin")
    if args.wine_user and not args.wine_prefix and platform.system() != "Linux":
        parser.error("--wine-user requires --wine-prefix")

    password = args.password
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            parser.error("--password-stdin received an empty password")
    if args.login:
        if not args.username:
            args.username = input("Reason Studios email/username: ").strip()
        if not password:
            password = getpass.getpass("Password: ")

    try:
        install = find_local_install(args.wine_prefix, args.wine_user)
    except RuntimeError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1

    if args.output:
        packs_dir = _expand_path(args.output)
    elif install["packs_dir"]:
        packs_dir = install["packs_dir"]
    else:
        print(
            "Error: could not find a Wine prefix with Reason Companion. Pass "
            "--wine-prefix PATH, or choose a folder with --output.",
            file=sys.stderr,
        )
        return 1

    # Registering installs with the account only makes sense when they land in
    # the Packs folder that this account's Companion uses.
    into_companion_packs = bool(install["packs_dir"]) and _same_path(packs_dir, install["packs_dir"])
    sync_cloud = not args.no_sync and into_companion_packs

    try:
        auth = SessionAuth(
            access_token=args.token, refresh_token=args.refresh_token,
            username=args.username, password=password,
            companion_db=install["leveldb"],
        )
        if auth.borrowed:
            print("[auth] Using the signed-in Companion session from {} (read-only).".format(install["label"]))
        packs = list_released_packs(auth)
    except Exception as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1

    packs = _selected_packs(packs, args.start, args.end)
    installed = get_installed_packs(packs_dir)
    print("Target directory : {}".format(packs_dir))
    print("Installed packs  : {}".format(len(installed)))
    print("Catalog packs    : {}".format(len(packs)))
    if not args.dry_run:
        if sync_cloud:
            print("Cloud sync       : on")
        elif args.no_sync:
            print("Cloud sync       : off (--no-sync)")
        else:
            print("Cloud sync       : off (target is not Companion's Packs folder)")
    print()

    stats = {
        "available": 0,
        "installed": 0,
        "skipped": 0,
        "unavailable": 0,
        "errors": 0,
    }
    aborted = None
    for index, pack in enumerate(packs):
        pack_id = pack["id"]
        title = pack["title"]
        print("[{}] {}".format(pack_id, title), end="")

        if title.casefold() in installed:
            print(" — already installed, skipping")
            stats["skipped"] += 1
            continue
        if args.dry_run:
            print(" — available")
            stats["available"] += 1
            continue

        if index:
            time.sleep(REQUEST_DELAY)
        try:
            download_url = fetch_download_url(pack_id, auth)
        except CompanionSessionExpired as error:
            print(" — stopped")
            aborted = error
            break
        except Exception as error:
            print(" — error retrieving download link: {}".format(error))
            stats["errors"] += 1
            continue
        if not download_url:
            print(" — not available to this account")
            stats["unavailable"] += 1
            continue

        print(" — downloading…")
        try:
            result = download_and_extract(
                pack_id, title, download_url, auth, packs_dir,
                sync_cloud=sync_cloud, verbose=args.verbose,
            )
        except NotEnoughSpace as error:
            print("  → stopped")
            aborted = error
            break
        except CompanionSessionExpired as error:
            if _pack_folder_exists(packs_dir, title):
                stats["installed"] += 1
                installed.add(title.casefold())
            aborted = error
            break
        except Exception as error:
            print("  → error: {}".format(error))
            stats["errors"] += 1
            continue
        if result == "skipped":
            print("  → already present")
            stats["skipped"] += 1
        else:
            print("  → installed ✓")
            stats["installed"] += 1
            installed.add(title.casefold())

    print("\nStopped early." if aborted else "\nComplete.")
    if args.dry_run:
        print("  Available   : {}".format(stats["available"]))
    else:
        print("  Installed   : {}".format(stats["installed"]))
    print("  Skipped     : {} (already installed)".format(stats["skipped"]))
    print("  Unavailable : {} (not entitled)".format(stats["unavailable"]))
    print("  Errors      : {}".format(stats["errors"]))
    if aborted:
        print("\n{}\nRun the script again to continue; installed packs are skipped.".format(aborted))
    return 1 if stats["errors"] or aborted else 0


if __name__ == "__main__":
    sys.exit(main())
