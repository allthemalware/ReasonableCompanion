import base64
import importlib.util
import json
import io
import os
import shutil
import time
import tempfile
import urllib.error
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "download_packs", PROJECT_ROOT / "download_packs.py"
)
download_packs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download_packs)


NO_INSTALL = {"label": None, "packs_dir": None, "leveldb": None}


class _Auth:
    borrowed = False
    refresh_token = None


def _set_mtime(path, value):
    for target in [path] + list(Path(path).iterdir()):
        os.utime(str(target), (value, value))


def _jwt(exp):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return "{}.{}.sig".format(part({"alg": "none"}), part({"exp": exp}))


def _write_zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


class DownloadPacksTests(unittest.TestCase):
    def test_catalog_is_paginated_and_sorted(self):
        pages = [
            {"totalMatches": 3, "hits": []},
            {"totalMatches": 3, "hits": [
                {"id": 9, "title": "Nine"}, {"id": 2, "title": "Two"},
            ]},
            {"totalMatches": 3, "hits": [{"id": 5, "title": "Five"}]},
        ]
        with mock.patch.object(download_packs, "api_get", side_effect=pages) as api_get:
            packs = download_packs.list_released_packs(object(), page_size=2)

        self.assertEqual(
            packs,
            [{"id": 2, "title": "Two"}, {"id": 5, "title": "Five"}, {"id": 9, "title": "Nine"}],
        )
        self.assertEqual(
            [call.args[0] for call in api_get.call_args_list],
            ["pack/", "pack/?q_page=1&q_size=2", "pack/?q_page=2&q_size=2"],
        )

    def test_full_catalog_list_is_used_when_available(self):
        catalog = [
            {"id": 9, "title": "Nine", "hasMetadataAvailable": True},
            {"id": 2, "title": "Two", "hasMetadataAvailable": False},
        ]
        with mock.patch.object(download_packs, "api_get", return_value=catalog) as api_get:
            packs = download_packs.list_released_packs(object())
        self.assertEqual(packs, [{"id": 2, "title": "Two"}, {"id": 9, "title": "Nine"}])
        api_get.assert_called_once_with("pack/", mock.ANY)

    def test_empty_catalog_is_an_error_not_a_clean_run(self):
        for response in ([], {"totalMatches": 0, "hits": []}):
            with mock.patch.object(download_packs, "api_get", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "empty"):
                    download_packs.list_released_packs(object())

    def test_catalog_rejects_unsafe_pack_title(self):
        response = [{"id": 1, "title": "../escape"}]
        with mock.patch.object(download_packs, "api_get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "unsafe title"):
                download_packs.list_released_packs(object())

    def test_api_retries_once_after_an_expired_access_token(self):
        class Auth:
            refresh_token = "refresh-token"

            def __init__(self):
                self.refresh_count = 0

            def headers(self):
                return {"Authorization": "Bearer access-token"}

            def renew(self):
                self.refresh_count += 1
                return True

        auth = Auth()
        expired = urllib.error.HTTPError("https://example.invalid", 401, "expired", {}, None)
        with mock.patch.object(
            download_packs.urllib.request,
            "urlopen",
            side_effect=[expired, io.BytesIO(b'{"ok": true}')],
        ) as urlopen:
            response = download_packs.api_get("example/", auth)

        self.assertEqual(response, {"ok": True})
        self.assertEqual(auth.refresh_count, 1)
        self.assertEqual(urlopen.call_count, 2)

    def test_wine_profile_prefers_the_user_with_companion_storage(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            prefix = Path(temporary_dir)
            steam_packs = prefix / "drive_c/users/steamuser/AppData/Roaming/Propellerhead Software/Packs"
            account_packs = prefix / "drive_c/users/account/AppData/Roaming/Propellerhead Software/Packs"
            companion = prefix / "drive_c/users/account/AppData/Roaming/Reason Companion/Local Storage/leveldb"
            legacy_companion = prefix / "drive_c/users/steamuser/AppData/Roaming/Reason+ Companion/Local Storage/leveldb"
            steam_packs.mkdir(parents=True)
            account_packs.mkdir(parents=True)
            companion.mkdir(parents=True)
            legacy_companion.mkdir(parents=True)
            _set_mtime(legacy_companion, 1000)
            _set_mtime(companion, 1000)

            install = download_packs.find_local_install(str(prefix))
            self.assertEqual(install["packs_dir"], str(account_packs))
            self.assertEqual(install["leveldb"], str(companion))

    def test_session_and_packs_come_from_the_same_profile(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            prefix = Path(temporary_dir)
            roaming = "AppData/Roaming"
            # "alpha" sorts first and has no Packs folder; "zulu" has Packs but
            # an older Companion session. Both values must come from one user.
            alpha_db = prefix / "drive_c/users/alpha" / roaming / "Reason Companion/Local Storage/leveldb"
            zulu_db = prefix / "drive_c/users/zulu" / roaming / "Reason Companion/Local Storage/leveldb"
            zulu_packs = prefix / "drive_c/users/zulu" / roaming / "Propellerhead Software/Packs"
            alpha_db.mkdir(parents=True)
            zulu_db.mkdir(parents=True)
            zulu_packs.mkdir(parents=True)
            _set_mtime(alpha_db, 1000)
            _set_mtime(zulu_db, 2000)

            install = download_packs.find_local_install(str(prefix))
            self.assertEqual(install["leveldb"], str(zulu_db))
            self.assertEqual(install["packs_dir"], str(zulu_packs))

            _set_mtime(alpha_db, 3000)
            install = download_packs.find_local_install(str(prefix))
            self.assertEqual(install["leveldb"], str(alpha_db))
            self.assertEqual(
                install["packs_dir"],
                str(prefix / "drive_c/users/alpha" / roaming / "Propellerhead Software/Packs"),
            )

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinked_wine_users_are_one_profile(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            users = Path(temporary_dir) / "drive_c/users"
            (users / "alice/AppData/Roaming/Reason Companion/Local Storage/leveldb").mkdir(parents=True)
            os.symlink("alice", str(users / "bob"))
            self.assertEqual(download_packs._wine_user_names(temporary_dir), ["alice"])
            self.assertEqual(download_packs._wine_user_names(temporary_dir, "BOB"), ["bob"])

    def test_leveldb_discovery_reads_ldb_files_and_either_token_order(self):
        with tempfile.TemporaryDirectory() as db_dir:
            token_file = Path(db_dir) / "000123.ldb"
            token_file.write_bytes(
                b"prefix{\"refreshToken\":\"refresh\",\"accessToken\":\"access\"}suffix"
            )
            self.assertEqual(
                download_packs.discover_session_tokens(db_dir), ("access", "refresh")
            )

    def test_archive_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = Path(temporary_dir) / "malicious.zip"
            staging_dir = Path(temporary_dir) / "staging"
            staging_dir.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "not allowed")

            with self.assertRaisesRegex(ValueError, "unsafe path"):
                download_packs.extract_archive(str(archive_path), str(staging_dir))
            self.assertFalse((Path(temporary_dir) / "outside.txt").exists())

    def test_archive_extraction_removes_macos_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = Path(temporary_dir) / "pack.zip"
            staging_dir = Path(temporary_dir) / "staging"
            staging_dir.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Pack/Preset.repatch", "preset")
                archive.writestr("__MACOSX/Pack/._Preset.repatch", "metadata")
                archive.writestr("Pack/.DS_Store", "metadata")

            download_packs.extract_archive(str(archive_path), str(staging_dir))
            self.assertEqual((staging_dir / "Pack/Preset.repatch").read_text(), "preset")
            self.assertFalse((staging_dir / "__MACOSX").exists())
            self.assertFalse((staging_dir / "Pack/.DS_Store").exists())

    def test_dry_run_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = Path(temporary_dir) / "new-packs-directory"
            packs = [{"id": 1, "title": "Available Pack"}]
            with mock.patch.object(download_packs, "SessionAuth", return_value=_Auth()), mock.patch.object(
                download_packs, "list_released_packs", return_value=packs
            ), mock.patch.object(download_packs, "find_local_install", return_value=NO_INSTALL):
                result = download_packs.main(["--token", "test-token", "--dry-run", "--output", str(output_dir)])

            self.assertEqual(result, 0)
            self.assertFalse(output_dir.exists())

    def test_selection_filters_are_inclusive(self):
        packs = [{"id": 1}, {"id": 2}, {"id": 3}]
        self.assertEqual(download_packs._selected_packs(packs, 2, 3), [{"id": 2}, {"id": 3}])


    def test_leveldb_discovery_prefers_the_newest_token_over_file_order(self):
        with tempfile.TemporaryDirectory() as db_dir:
            newer, older = _jwt(time.time() + 200), _jwt(time.time() - 200)
            first = Path(db_dir) / "000001.ldb"
            second = Path(db_dir) / "000002.log"
            first.write_bytes('{{"accessToken":"{}","refreshToken":"r-new"}}'.format(newer).encode())
            second.write_bytes('{{"accessToken":"{}","refreshToken":"r-old"}}'.format(older).encode())
            os.utime(str(first), (1000, 1000))
            os.utime(str(second), (2000, 2000))
            self.assertEqual(download_packs.discover_session_tokens(db_dir), (newer, "r-new"))

    def test_borrowed_companion_session_is_never_renewed(self):
        with tempfile.TemporaryDirectory() as db_dir:
            (Path(db_dir) / "000001.log").write_bytes(
                '{{"accessToken":"{}","refreshToken":"companion-refresh"}}'.format(
                    _jwt(time.time() - 60)
                ).encode()
            )
            with mock.patch.object(download_packs.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(download_packs.CompanionSessionExpired):
                    download_packs.SessionAuth(companion_db=db_dir)
            urlopen.assert_not_called()

    def test_borrowed_session_reloads_storage_after_401_without_refreshing(self):
        with tempfile.TemporaryDirectory() as db_dir:
            token_file = Path(db_dir) / "000001.log"
            first, second = _jwt(time.time() + 100), _jwt(time.time() + 200)
            token_file.write_bytes('{{"accessToken":"{}","refreshToken":"r"}}'.format(first).encode())
            auth = download_packs.SessionAuth(companion_db=db_dir)
            self.assertTrue(auth.borrowed)
            self.assertIsNone(auth.refresh_token)
            with self.assertRaisesRegex(RuntimeError, "Refusing"):
                auth.refresh()

            unauthorized = urllib.error.HTTPError("https://example.invalid", 401, "expired", {}, None)
            # Storage unchanged: give up rather than renew Companion's session.
            with mock.patch.object(download_packs.urllib.request, "urlopen", side_effect=[unauthorized]):
                with self.assertRaises(download_packs.CompanionSessionExpired):
                    download_packs.api_get("example/", auth)

            # Companion renewed its session meanwhile: use the new token.
            token_file.write_bytes('{{"accessToken":"{}","refreshToken":"r"}}'.format(second).encode())
            with mock.patch.object(
                download_packs.urllib.request, "urlopen",
                side_effect=[unauthorized, io.BytesIO(b'{"ok": true}')],
            ) as urlopen:
                self.assertEqual(download_packs.api_get("example/", auth), {"ok": True})
            self.assertEqual(urlopen.call_args.args[0].get_header("Authorization"), "Bearer " + second)
            self.assertNotIn("/auth/", urlopen.call_args.args[0].full_url)

    def test_catalog_that_repeats_a_page_does_not_loop_forever(self):
        page = {"totalMatches": 3, "hits": [{"id": 1, "title": "One"}]}
        with mock.patch.object(download_packs, "api_get", return_value=page) as api_get:
            with self.assertRaisesRegex(RuntimeError, "no new packs"):
                download_packs.list_released_packs(object(), page_size=1)
        self.assertEqual(api_get.call_count, 3)

    def _install(self, root, members, title="New Pack"):
        packs_dir = Path(root) / "Propellerhead Software" / "Packs"
        source_zip = Path(root) / "source.zip"
        _write_zip(source_zip, members)
        seen_dirs = []

        def fake_download(url, destination, verbose):
            seen_dirs.append(os.path.dirname(destination))
            shutil.copyfile(str(source_zip), destination)

        with mock.patch.object(download_packs, "_download_to_file", side_effect=fake_download), \
                mock.patch.object(download_packs, "fetch_pack_rsmeta", return_value={"id": 7}), \
                mock.patch.object(download_packs, "register_installed_pack") as register:
            result = download_packs.download_and_extract(
                7, title, "https://example.invalid/p.zip", _Auth(), str(packs_dir), sync_cloud=True,
            )
        return packs_dir, result, seen_dirs, register

    def test_install_stages_beside_packs_and_keeps_the_archive_layout(self):
        with tempfile.TemporaryDirectory() as root:
            packs_dir, result, seen_dirs, register = self._install(
                root, {"New Pack/Patches/a.cmb": "a", "New Pack/demo.mp3": "b"}
            )
            self.assertEqual(result, "installed")
            # Same layout as Companion: the archive's top folder is kept.
            self.assertEqual((packs_dir / "New Pack/New Pack/Patches/a.cmb").read_text(), "a")
            self.assertEqual(json.loads((packs_dir / "New Pack/.rsmeta").read_text()), {"id": 7})
            register.assert_called_once()
            # Work files lived next to Packs (same disk, invisible to Reason)
            # and are gone afterwards.
            self.assertEqual(Path(seen_dirs[0]).parent, packs_dir.parent)
            self.assertEqual(sorted(os.listdir(str(packs_dir))), ["New Pack"])
            self.assertEqual(sorted(os.listdir(str(packs_dir.parent))), ["Packs"])

    def test_install_skips_an_existing_folder_with_different_case(self):
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root) / "Propellerhead Software" / "Packs" / "new pack"
            existing.mkdir(parents=True)
            (existing / "keep.txt").write_text("original")
            packs_dir, result, seen_dirs, register = self._install(root, {"x.cmb": "x"})
            self.assertEqual(result, "skipped")
            self.assertEqual(os.listdir(str(packs_dir)), ["new pack"])
            self.assertEqual((existing / "keep.txt").read_text(), "original")
            self.assertEqual(seen_dirs, [])
            register.assert_not_called()

    def test_install_does_not_merge_into_a_folder_created_meanwhile(self):
        with tempfile.TemporaryDirectory() as root:
            packs_dir = Path(root) / "Propellerhead Software" / "Packs"
            original_extract = download_packs.extract_archive

            def extract_then_companion_installs(archive_path, staging_dir):
                original_extract(archive_path, staging_dir)
                (packs_dir / "New Pack").mkdir()

            with mock.patch.object(download_packs, "extract_archive", side_effect=extract_then_companion_installs):
                packs_dir, result, _, register = self._install(root, {"x.cmb": "x"})
            self.assertEqual(result, "skipped")
            self.assertEqual(os.listdir(str(packs_dir / "New Pack")), [])
            self.assertEqual(sorted(os.listdir(str(packs_dir.parent))), ["Packs"])
            register.assert_not_called()

    def test_cloud_sync_is_off_when_output_is_not_companions_packs_folder(self):
        with tempfile.TemporaryDirectory() as root:
            companion_packs = Path(root) / "Packs"
            other = Path(root) / "Elsewhere"
            install = {"label": "test", "packs_dir": str(companion_packs), "leveldb": None}
            calls = []

            def fake_install(*args, **kwargs):
                calls.append(kwargs["sync_cloud"])
                return "installed"

            with mock.patch.object(download_packs, "find_local_install", return_value=install), \
                    mock.patch.object(download_packs, "SessionAuth", return_value=_Auth()), \
                    mock.patch.object(download_packs, "list_released_packs", return_value=[{"id": 1, "title": "P"}]), \
                    mock.patch.object(download_packs, "fetch_download_url", return_value="https://x.invalid"), \
                    mock.patch.object(download_packs, "download_and_extract", side_effect=fake_install):
                download_packs.main(["--token", "t", "-o", str(other)])
                download_packs.main(["--token", "t", "-o", str(companion_packs)])
                download_packs.main(["--token", "t"])
                download_packs.main(["--token", "t", "--no-sync"])
            self.assertEqual(calls, [False, True, True, False])

    def test_no_install_found_requires_an_explicit_target(self):
        with mock.patch.object(download_packs, "find_local_install", return_value=NO_INSTALL), \
                mock.patch.object(download_packs, "SessionAuth") as session:
            self.assertEqual(download_packs.main(["--token", "t", "--dry-run"]), 1)
        session.assert_not_called()


    def test_not_enough_space_stops_before_writing_the_archive(self):
        with tempfile.TemporaryDirectory() as root:
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {"Content-Length": str(10 << 30)}
            usage = mock.Mock(free=5 << 30)
            destination = os.path.join(root, "pack.zip")
            with mock.patch.object(download_packs.urllib.request, "urlopen", return_value=response), \
                    mock.patch.object(download_packs.shutil, "disk_usage", return_value=usage):
                with self.assertRaises(download_packs.NotEnoughSpace):
                    download_packs._download_to_file("https://x.invalid", destination, False)
            self.assertFalse(os.path.exists(destination))
            response.read.assert_not_called()


    def test_output_in_a_legacy_code_page_does_not_crash(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        packs = [{"id": 1, "title": "Snowman \u2603 Pack"}]
        with mock.patch.object(download_packs.sys, "stdout", stream), \
                mock.patch.object(download_packs, "find_local_install", return_value=NO_INSTALL), \
                mock.patch.object(download_packs, "SessionAuth", return_value=_Auth()), \
                mock.patch.object(download_packs, "list_released_packs", return_value=packs), \
                mock.patch.object(download_packs, "fetch_download_url", return_value="https://x.invalid"), \
                mock.patch.object(download_packs, "download_and_extract", return_value="installed"):
            with tempfile.TemporaryDirectory() as root:
                result = download_packs.main(["--token", "t", "-o", root])
        self.assertEqual(result, 0)
        stream.flush()
        self.assertIn(b"installed ?", stream.buffer.getvalue())


    def test_windows_invalid_characters_in_archive_names_are_replaced(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = Path(temporary_dir) / "pack.zip"
            target = Path(temporary_dir) / "target"
            target.mkdir()
            _write_zip(archive_path, {
                "Patches/Fx 1:4 Pong Bouncer.echo": "echo",
                "01_PATCHES/Clap:Snare Bank.cmb": "cmb",
                "Odd|name?.txt": "odd",
            })
            download_packs.extract_archive(str(archive_path), str(target))
            self.assertEqual((target / "Patches/Fx 1-4 Pong Bouncer.echo").read_text(), "echo")
            self.assertEqual((target / "01_PATCHES/Clap-Snare Bank.cmb").read_text(), "cmb")
            self.assertEqual((target / "Odd-name-.txt").read_text(), "odd")

    def test_names_that_collide_after_replacement_are_an_error(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = Path(temporary_dir) / "pack.zip"
            target = Path(temporary_dir) / "target"
            target.mkdir()
            _write_zip(archive_path, {"a:b.cmb": "first", "a-b.cmb": "second"})
            with self.assertRaisesRegex(ValueError, "same file"):
                download_packs.extract_archive(str(archive_path), str(target))
            self.assertEqual(os.listdir(str(target)), [])

    def test_symbolic_links_in_archives_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = Path(temporary_dir) / "pack.zip"
            target = Path(temporary_dir) / "target"
            target.mkdir()
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(info, "/etc/passwd")
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                download_packs.extract_archive(str(archive_path), str(target))
            self.assertEqual(os.listdir(str(target)), [])


if __name__ == "__main__":
    unittest.main()
