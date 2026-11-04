from __future__ import annotations

import os
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from github_api import (
    MAX_FILE_SIZE,
    GitHubAPI,
    GitHubError,
    Repository,
    RepositoryFile,
    authenticate_github,
    scan_local_files,
    validate_repository_path,
)
from main import GithubManagerWindow
from PySide6.QtWidgets import QApplication


_QT_APP = QApplication.instance() or QApplication([])


class RepositoryPathTests(unittest.TestCase):
    def test_accepts_nested_repository_path(self):
        self.assertEqual(validate_repository_path("src/widgets/main.py"), ("src", "widgets", "main.py"))

    def test_rejects_traversal_and_separator_confusion(self):
        for path in ("", "../outside", "folder/../../outside", "/absolute", "folder\\outside", "a//b"):
            with self.subTest(path=path), self.assertRaises(GitHubError):
                validate_repository_path(path)

    @unittest.skipUnless(os.name == "nt", "Windows-specific filesystem names")
    def test_rejects_windows_device_names(self):
        for path in ("CON", "folder/NUL.txt", "name.", "file:stream"):
            with self.subTest(path=path), self.assertRaises(GitHubError):
                validate_repository_path(path)


class LocalFolderTests(unittest.TestCase):
    def test_scanner_skips_hidden_git_symlink_and_package_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            (root / "visible.txt").write_text("ok", encoding="utf-8")
            (root / ".hidden").write_text("hidden", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("secret", encoding="utf-8")
            (root / "Widget.app").mkdir()
            (root / "Widget.app" / "payload").write_text("package", encoding="utf-8")
            outside = Path(temporary) / "outside.txt"
            outside.write_text("external", encoding="utf-8")
            try:
                (root / "link.txt").symlink_to(outside)
            except (OSError, NotImplementedError):
                pass

            self.assertEqual(
                scan_local_files(root),
                [("visible.txt", root / "visible.txt")],
            )

    def test_rejects_file_as_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            file_path = Path(temporary) / "file"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(GitHubError):
                scan_local_files(file_path)


class GitHubOperationTests(unittest.TestCase):
    def setUp(self):
        self.api = GitHubAPI()
        self.repo = Repository("owner/project", True, "main")

    def test_upload_existing_branch_uses_non_force_ref_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "readme.txt"
            path.write_bytes(b"hello")
            response = Mock(
                side_effect=[
                    {"object": {"sha": "parent"}},
                    {"tree": {"sha": "base-tree"}},
                    {"sha": "blob"},
                    {"sha": "new-tree"},
                    {"sha": "new-commit"},
                    {},
                ]
            )
            self.api._request = response
            result = self.api.upload_files(
                "not-logged-token",
                self.repo,
                [("readme.txt", path)],
                "Add readme",
            )
            self.assertEqual(result, "new-commit")
            ref_call = response.call_args_list[-1]
            self.assertEqual(ref_call.args[0], "PATCH")
            self.assertTrue(ref_call.args[3]["force"] is False)
            self.assertEqual(ref_call.args[2], "not-logged-token")

    def test_upload_initializes_empty_repository_with_contents_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "first.txt"
            path.write_bytes(b"first")
            response = Mock(
                side_effect=[
                    GitHubError("empty repository", 409),
                    {"commit": {"sha": "initial-commit"}},
                ]
            )
            self.api._request = response
            result = self.api.upload_files(
                "token", self.repo, [("first.txt", path)], "Start project"
            )
            self.assertEqual(result, "initial-commit")
            self.assertEqual(response.call_args_list[1].args[0], "PUT")
            self.assertIn("contents/first.txt", response.call_args_list[1].args[1])

    def test_upload_enforces_per_file_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large"
            with path.open("wb") as stream:
                stream.truncate(MAX_FILE_SIZE + 1)
            self.api._request = Mock()
            with self.assertRaisesRegex(GitHubError, "100 MB"):
                self.api.upload_files("token", self.repo, [("large", path)], "message")
            self.api._request.assert_not_called()

    def test_download_writes_atomically_and_replaces_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "nested" / "value.txt"
            target.parent.mkdir()
            target.write_text("old", encoding="utf-8")
            GitHubAPI._write_downloaded_file(root, "nested/value.txt", b"new")
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(any(path.name.startswith(".github-manager-") for path in root.rglob("*")))

    def test_download_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symbolic link creation is not available.")
            with self.assertRaises(GitHubError):
                GitHubAPI._write_downloaded_file(root, "link/file.txt", b"data")
            self.assertFalse((outside / "file.txt").exists())

    def test_update_visibility_uses_repository_endpoint(self):
        self.api._request = Mock(return_value={
            "full_name": "owner/project",
            "private": False,
            "default_branch": "main",
        })
        result = self.api.update_visibility("token", self.repo, False)
        self.assertFalse(result.private)
        self.api._request.assert_called_once_with(
            "PATCH",
            "repos/owner/project",
            "token",
            {"private": False},
        )


class GitHubConnectionTests(unittest.TestCase):
    class _Response:
        def __init__(self, value: dict | list):
            self.value = json.dumps(value).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self.value

    def test_authenticated_repository_request_sends_bearer_token(self):
        response = self._Response(
            [{"full_name": "owner/private", "private": True, "default_branch": "main"}]
        )
        with patch("github_api.urllib.request.urlopen", return_value=response) as urlopen:
            repos = GitHubAPI().list_repositories("mock-token")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer mock-token")
        self.assertTrue(request.full_url.endswith("/user/repos?per_page=100&sort=full_name&page=1"))
        self.assertEqual([repo.full_name for repo in repos], ["owner/private"])

    def test_device_authorization_requests_required_scopes(self):
        payload = {
            "device_code": "mock-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        with patch(
            "github_api.urllib.request.urlopen",
            return_value=self._Response(payload),
        ) as urlopen:
            result = GitHubAPI().request_device_authorization("mock-client-id")
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/login/device/code"))
        self.assertEqual(request.get_method(), "POST")
        self.assertIn(b"scope=repo+read%3Auser", request.data)
        self.assertEqual(result.user_code, "ABCD-EFGH")

    def test_successful_mock_device_flow_loads_repositories_read_only(self):
        authorization = {
            "device_code": "mock-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 10,
            "interval": 1,
        }
        token = {"access_token": "mock-access-token", "token_type": "bearer"}
        user = {"login": "test-account"}
        repositories = [
            {"full_name": "test-account/repo", "private": True, "default_branch": "main"}
        ]
        responses = [
            self._Response(authorization),
            self._Response(token),
            self._Response(user),
            self._Response(repositories),
        ]
        api = GitHubAPI()
        saved = []
        with (
            patch("github_api.urllib.request.urlopen", side_effect=responses) as urlopen,
            patch("github_api.time.sleep"),
        ):
            access_token, account, repos, error = authenticate_github(
                api,
                "mock-client-id",
                saved.append,
            )
        self.assertIsNone(error)
        self.assertEqual(saved, ["mock-access-token"])
        self.assertEqual(account["login"], "test-account")
        self.assertEqual([repo.full_name for repo in repos], ["test-account/repo"])
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(
            requests[-2].get_header("Authorization"),
            "Bearer mock-access-token",
        )
        self.assertEqual(
            requests[-1].get_header("Authorization"),
            "Bearer mock-access-token",
        )
        self.assertTrue(requests[-1].full_url.endswith("/user/repos?per_page=100&sort=full_name&page=1"))

    def test_device_token_is_saved_after_validation_even_if_repository_load_fails(self):
        api = Mock()
        api.request_device_authorization.return_value = object()
        api.poll_device_authorization.return_value = "mock-access-token"
        api.get_user.return_value = {"login": "test-account"}
        saved = []

        def list_repositories(token):
            self.assertEqual(token, "mock-access-token")
            self.assertEqual(saved, ["mock-access-token"])
            raise GitHubError("temporary repository-list network failure")

        api.list_repositories.side_effect = list_repositories
        token, user, repos, error = authenticate_github(
            api, "mock-client-id", saved.append
        )
        self.assertEqual(token, "mock-access-token")
        self.assertEqual(user["login"], "test-account")
        self.assertEqual(repos, [])
        self.assertIsInstance(error, GitHubError)
        self.assertIn("temporary repository-list", str(error))
        api.get_user.assert_called_once_with("mock-access-token")
        api.list_repositories.assert_called_once_with("mock-access-token")

    def test_rate_limit_failure_has_actionable_message(self):
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        error = HTTPError(
            "https://api.github.com/user/repos",
            403,
            "Forbidden",
            headers,
            io.BytesIO(b'{"message":"API rate limit exceeded"}'),
        )
        with patch("github_api.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(GitHubError, "rate limit") as raised:
                GitHubAPI().list_repositories("mock-token")
        self.assertEqual(raised.exception.status, 403)

    def test_missing_repository_scope_failure_explains_oauth_permissions(self):
        error = HTTPError(
            "https://api.github.com/user/repos",
            403,
            "Forbidden",
            Message(),
            io.BytesIO(b'{"message":"Resource not accessible by integration"}'),
        )
        with patch("github_api.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(GitHubError, "repo scope") as raised:
                GitHubAPI().list_repositories("mock-token")
        self.assertEqual(raised.exception.status, 403)

    def test_bad_oauth_client_id_points_to_device_flow_setting(self):
        error = HTTPError(
            "https://github.com/login/device/code",
            404,
            "Not Found",
            Message(),
            io.BytesIO(b'{"error":"bad_verification_code","error_description":"Not Found"}'),
        )
        with patch("github_api.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(GitHubError, "Device Flow") as raised:
                GitHubAPI().request_device_authorization("mock-client-id")
        self.assertEqual(raised.exception.status, 404)


class RepositoryDropdownTests(unittest.TestCase):
    def setUp(self):
        self.window = GithubManagerWindow(restore_credentials=False)
        self.window.token = "test-token"
        self.window.repositories = [
            Repository("owner/alpha", True, "main"),
            Repository("owner/beta", False, "trunk"),
        ]
        self.window._populate_repositories()

    def tearDown(self):
        self.window.close()

    def test_repository_picker_is_a_dropdown_and_selects_repository(self):
        picker = self.window.repository_combo
        self.assertFalse(picker.isEditable())
        self.assertEqual(picker.count(), 2)
        self.assertEqual(picker.maxVisibleItems(), 20)
        picker.setCurrentIndex(1)
        self.assertEqual(self.window._repository().full_name, "owner/beta")
        self.assertTrue(self.window.download_button.isEnabled())
        self.assertEqual(self.window.visibility_button.text(), "Make private…")

    def test_search_temporarily_hides_and_restores_repository_selection(self):
        picker = self.window.repository_combo
        picker.setCurrentIndex(1)
        self.window.search_repositories.setText("owner/alpha")
        self.assertEqual(picker.count(), 1)
        self.assertIsNone(self.window._repository())
        picker.setCurrentIndex(0)
        self.assertEqual(picker.currentData(), "owner/alpha")
        self.assertEqual(self.window._repository().full_name, "owner/alpha")
        self.window.search_repositories.setText("no-such-repository")
        self.assertEqual(picker.count(), 0)
        self.assertEqual(
            picker.placeholderText(),
            "No matches — change your search",
        )
        self.assertIsNone(self.window._repository())
        self.assertFalse(self.window.download_button.isEnabled())
        self.window.search_repositories.clear()
        self.assertEqual(picker.currentData(), "owner/alpha")
        self.assertEqual(self.window._repository().full_name, "owner/alpha")

    def test_dropdown_has_clear_empty_and_loading_states(self):
        self.window.token = None
        self.assertEqual(
            self.window._repository_placeholder(),
            "Connect GitHub to load repositories",
        )
        self.window._repositories_error = "authorization failed"
        self.assertEqual(
            self.window._repository_placeholder(),
            "Could not load repositories — reconnect to GitHub",
        )
        self.window.token = "test-token"
        self.window.repositories = []
        self.window._repositories_error = None
        self.assertEqual(self.window._repository_placeholder(), "No repositories available")
        self.window._repositories_loading = True
        self.assertEqual(self.window._repository_placeholder(), "Loading repositories…")
        self.window._repositories_loading = False
        self.window._repositories_error = "network failed"
        self.assertEqual(
            self.window._repository_placeholder(),
            "Repository load failed — refresh to retry",
        )



    def test_worker_passes_progress_signal_when_expected(self):
        from main import Worker
        progress_calls = []

        def task_with_progress(progress):
            progress("step 1")
            progress("step 2")
            return "done"

        worker = Worker(task_with_progress)
        worker.signals.progress.connect(progress_calls.append)
        results = []
        errors = []
        worker.signals.result.connect(results.append)
        worker.signals.error.connect(errors.append)
        worker.run()
        self.assertEqual(results, ["done"])
        self.assertEqual(errors, [])
        self.assertEqual(progress_calls, ["step 1", "step 2"])

    def test_worker_runs_no_arg_task_cleanly(self):
        from main import Worker

        def task_no_args():
            return "success"

        worker = Worker(task_no_args)
        results = []
        errors = []
        worker.signals.result.connect(results.append)
        worker.signals.error.connect(errors.append)
        worker.run()
        self.assertEqual(results, ["success"])
        self.assertEqual(errors, [])

    def test_authorize_worker_execution_does_not_fail_with_missing_progress(self):
        win = GithubManagerWindow(restore_credentials=False)
        with patch("main.authenticate_github") as mock_auth:
            mock_auth.return_value = ("token123", {"login": "testuser"}, [], None)
            win._authorize("mock_client_id")
            # The worker was submitted to threadpool
            self.assertTrue(len(win._workers) > 0)
            worker = win._workers[-1]
            # Run the worker directly to verify it executes without argument errors
            worker.run()
            self.assertEqual(win.token, "token123")
            self.assertEqual(win.account_name, "testuser")
        win.close()



    def test_repository_dropdown_items_have_checkboxes_and_sync_active_selection(self):
        from PySide6.QtCore import Qt
        picker = self.window.repository_combo
        model = picker.model()
        self.assertEqual(model.rowCount(), 2)
        item0 = model.item(0)
        item1 = model.item(1)
        self.assertTrue(item0.isCheckable())
        self.assertTrue(item1.isCheckable())

        # Select first item
        picker.setCurrentIndex(0)
        self.assertEqual(item0.checkState(), Qt.CheckState.Checked)
        self.assertEqual(item1.checkState(), Qt.CheckState.Unchecked)

        # Select second item
        picker.setCurrentIndex(1)
        self.assertEqual(item0.checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(item1.checkState(), Qt.CheckState.Checked)


if __name__ == "__main__":
    unittest.main()
