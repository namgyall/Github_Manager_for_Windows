from __future__ import annotations

import base64
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


API_ROOT = "https://api.github.com"
DEVICE_ROOT = "https://github.com/login"
MAX_FILE_SIZE = 100 * 1024 * 1024
ProgressCallback = Callable[[str], None]


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Repository:
    full_name: str
    private: bool
    default_branch: str

    @classmethod
    def from_json(cls, value: dict) -> "Repository":
        name = value.get("full_name")
        if not isinstance(name, str) or not re.fullmatch(r"[^/]+/[^/]+", name):
            raise GitHubError("GitHub returned an invalid repository name.")
        return cls(
            full_name=name,
            private=bool(value.get("private")),
            default_branch=value.get("default_branch") or "main",
        )


@dataclass(frozen=True)
class RepositoryFile:
    path: str
    sha: str
    size: int | None = None


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def validate_repository_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        raise GitHubError(f"Unsafe repository path: {path!r}")
    if "\\" in path or "\x00" in path:
        raise GitHubError(f"Unsafe repository path: {path!r}")

    parts = tuple(path.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise GitHubError(f"Unsafe repository path: {path!r}")

    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved.update(f"COM{index}" for index in range(1, 10))
        reserved.update(f"LPT{index}" for index in range(1, 10))
        for part in parts:
            stem = part.split(".", 1)[0].rstrip(" .").upper()
            if (
                any(ord(char) < 32 for char in part)
                or any(char in '<>:"|?*' for char in part)
                or part.endswith((".", " "))
                or stem in reserved
            ):
                raise GitHubError(f"Unsafe Windows file path: {path!r}")
    return parts


def scan_local_files(folder: Path) -> list[tuple[str, Path]]:
    root = folder.resolve(strict=True)
    if not root.is_dir():
        raise GitHubError("Choose a local folder before scanning files.")

    files: list[tuple[str, Path]] = []
    package_extensions = {".app", ".bundle", ".pkg"}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in directories:
            candidate = current_path / name
            try:
                details = candidate.lstat()
            except OSError:
                continue
            if (
                name.startswith(".")
                or name == ".git"
                or stat.S_ISLNK(details.st_mode)
                or _is_hidden(details)
                or candidate.suffix.lower() in package_extensions
                or _is_junction(candidate)
            ):
                continue
            kept_directories.append(name)
        directories[:] = kept_directories

        for name in filenames:
            if name.startswith("."):
                continue
            candidate = current_path / name
            try:
                details = candidate.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(details.st_mode) or _is_hidden(details):
                continue
            relative = candidate.relative_to(root).as_posix()
            files.append((relative, candidate))
    return sorted(files, key=lambda item: item[0].casefold())


def _is_hidden(details: os.stat_result) -> bool:
    hidden_flag = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(attributes & hidden_flag)


def _is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return is_junction()
    except OSError:
        return True


def _read_regular_file(path: Path, relative: str) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise GitHubError(f"Not a regular file: {relative}")
        if before.st_size > MAX_FILE_SIZE:
            raise GitHubError(f"{relative} is larger than the 100 MB per-file limit.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise GitHubError(f"File changed while it was being opened: {relative}")
            content = bytearray()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_FILE_SIZE:
                    raise GitHubError(
                        f"{relative} is larger than the 100 MB per-file limit."
                    )
            return bytes(content)
        finally:
            os.close(descriptor)
    except GitHubError:
        raise
    except OSError as error:
        raise GitHubError(f"Could not read {relative}: {error}") from None


class GitHubAPI:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    @staticmethod
    def _json_body(data: bytes):
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict | None = None,
        *,
        base: str = API_ROOT,
        accept: str = "application/vnd.github+json",
    ):
        if path.startswith(("/", "\\")) or "://" in path:
            raise GitHubError("Invalid GitHub API path.")
        request = urllib.request.Request(
            f"{base}/{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={
                "Accept": accept,
                "User-Agent": "GithubManager-PySide6",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {token}"} if token else {}),
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            message = self._json_body(raw)
            detail = message.get("message") if isinstance(message, dict) else None
            if error.code == 401:
                raise GitHubError("GitHub rejected the token. Sign in again.", error.code) from None
            remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
            if error.code == 403 and remaining == "0":
                raise GitHubError("GitHub API rate limit reached. Try again later.", error.code) from None
            if error.code == 403:
                hint = (
                    "Check that the OAuth app was granted the repo scope and that "
                    "organization policies allow it."
                )
                raise GitHubError(
                    f"GitHub denied this request. {hint}"
                    + (f" GitHub said: {detail}" if detail else ""),
                    error.code,
                ) from None
            raise GitHubError(
                f"GitHub returned HTTP {error.code}: {detail or error.reason}",
                error.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GitHubError(f"Could not reach GitHub: {error}") from None
        if not data:
            return None
        result = self._json_body(data)
        if result is None:
            raise GitHubError("GitHub returned an invalid response.")
        return result

    def request_device_authorization(self, client_id: str) -> DeviceAuthorization:
        body = urllib.parse.urlencode(
            {"client_id": client_id.strip(), "scope": "repo read:user"}
        ).encode("ascii")
        request = urllib.request.Request(
            f"{DEVICE_ROOT}/device/code",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "GithubManager-PySide6",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = self._json_body(response.read())
        except urllib.error.HTTPError as error:
            result = self._json_body(error.read())
            description = result.get("error_description") if isinstance(result, dict) else None
            if error.code in (400, 404, 422):
                raise GitHubError(
                    "GitHub rejected the OAuth App Client ID. Check that it is the "
                    "Client ID (not a client secret) and that Device Flow is enabled "
                    "in the OAuth App settings."
                    + (f" GitHub said: {description}" if description else ""),
                    error.code,
                ) from None
            raise GitHubError(description or f"GitHub returned HTTP {error.code}.", error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GitHubError(f"Could not start GitHub authorization: {error}") from None
        if not isinstance(result, dict) or result.get("error"):
            message = result.get("error_description") if isinstance(result, dict) else None
            raise GitHubError(message or "GitHub could not start device authorization.")
        try:
            verification_uri = result["verification_uri"]
            parsed_uri = urllib.parse.urlparse(verification_uri)
            if parsed_uri.scheme != "https" or parsed_uri.hostname not in (
                "github.com",
                "www.github.com",
            ):
                raise ValueError
            return DeviceAuthorization(
                device_code=result["device_code"],
                user_code=result["user_code"],
                verification_uri=verification_uri,
                expires_in=int(result["expires_in"]),
                interval=max(1, int(result.get("interval", 5))),
            )
        except (KeyError, TypeError, ValueError):
            raise GitHubError("GitHub returned an invalid device authorization response.") from None

    def poll_device_authorization(
        self,
        authorization: DeviceAuthorization,
        client_id: str,
        *,
        progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        deadline = time.monotonic() + authorization.expires_in
        interval = max(1, authorization.interval)
        while time.monotonic() < deadline:
            remaining = interval
            while remaining > 0:
                if cancelled and cancelled():
                    raise GitHubError("Authorization cancelled.")
                sleep_for = min(0.25, remaining)
                time.sleep(sleep_for)
                remaining -= sleep_for
            body = urllib.parse.urlencode(
                {
                    "client_id": client_id.strip(),
                    "device_code": authorization.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
            ).encode("ascii")
            request = urllib.request.Request(
                f"{DEVICE_ROOT}/oauth/access_token",
                data=body,
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "GithubManager-PySide6",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = self._json_body(response.read())
            except urllib.error.HTTPError as error:
                result = self._json_body(error.read())
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise GitHubError(f"GitHub authorization could not be checked: {error}") from None
            if not isinstance(result, dict):
                raise GitHubError("GitHub returned an invalid authorization response.")
            if result.get("access_token"):
                return result["access_token"]
            error = result.get("error")
            if error == "authorization_pending":
                if progress:
                    progress("Waiting for you to approve access on GitHub…")
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "access_denied":
                raise GitHubError("GitHub authorization was denied.")
            if error == "expired_token":
                raise GitHubError("The device code expired. Start authorization again.")
            description = result.get("error_description")
            if error == "incorrect_client_credentials":
                raise GitHubError(
                    "GitHub rejected the OAuth App Client ID. Check that you entered "
                    "the Client ID and enabled Device Flow in the OAuth App settings."
                )
            raise GitHubError(description or "GitHub authorization failed.")
        raise GitHubError("The device code expired. Start authorization again.")

    def get_user(self, token: str) -> dict:
        return self._request("GET", "user", token)

    def list_repositories(self, token: str) -> list[Repository]:
        page = 1
        repositories: list[Repository] = []
        while True:
            result = self._request(
                "GET",
                f"user/repos?per_page=100&sort=full_name&page={page}",
                token,
            )
            if not isinstance(result, list):
                raise GitHubError("GitHub returned an invalid repository list.")
            repositories.extend(Repository.from_json(item) for item in result)
            if len(result) < 100:
                return sorted(repositories, key=lambda repo: repo.full_name.casefold())
            page += 1

    def create_repository(
        self, token: str, name: str, description: str, private: bool
    ) -> Repository:
        result = self._request(
            "POST",
            "user/repos",
            token,
            {
                "name": name,
                "description": description,
                "private": private,
                "auto_init": True,
            },
        )
        return Repository.from_json(result)

    def update_visibility(self, token: str, repo: Repository, private: bool) -> Repository:
        result = self._request(
            "PATCH",
            f"repos/{self._repo_path(repo)}",
            token,
            {"private": private},
        )
        return Repository.from_json(result)

    def list_branches(self, token: str, repo: Repository) -> list[str]:
        page = 1
        names: list[str] = []
        while True:
            result = self._request(
                "GET",
                f"repos/{self._repo_path(repo)}/branches?per_page=100&page={page}",
                token,
            )
            if not isinstance(result, list):
                raise GitHubError("GitHub returned an invalid branch list.")
            names.extend(branch["name"] for branch in result if isinstance(branch.get("name"), str))
            if len(result) < 100:
                return names
            page += 1

    def list_files(self, token: str, repo: Repository, branch: str) -> list[RepositoryFile]:
        encoded_branch = urllib.parse.quote(branch, safe="/-._~")
        reference = self._request(
            "GET",
            f"repos/{self._repo_path(repo)}/git/ref/heads/{encoded_branch}",
            token,
        )
        commit_sha = reference.get("object", {}).get("sha")
        if not commit_sha:
            raise GitHubError("GitHub returned an invalid branch reference.")
        tree = self._request(
            "GET",
            f"repos/{self._repo_path(repo)}/git/trees/{urllib.parse.quote(commit_sha, safe='')}?recursive=1",
            token,
        )
        if tree.get("truncated"):
            raise GitHubError(
                "This repository tree is too large to browse in one request. Try a smaller repository."
            )
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise GitHubError("GitHub returned an invalid repository tree.")
        files: list[RepositoryFile] = []
        for entry in entries:
            if entry.get("type") != "blob":
                continue
            parts = validate_repository_path(entry.get("path"))
            if entry.get("mode") == "120000":
                continue
            files.append(
                RepositoryFile(
                    path="/".join(parts),
                    sha=entry.get("sha", ""),
                    size=entry.get("size"),
                )
            )
        return sorted(files, key=lambda file: file.path.casefold())

    def delete_file(
        self, token: str, repo: Repository, branch: str, file: RepositoryFile
    ) -> str:
        parts = validate_repository_path(file.path)
        encoded_path = "/".join(urllib.parse.quote(part, safe="-._~") for part in parts)
        result = self._request(
            "DELETE",
            f"repos/{self._repo_path(repo)}/contents/{encoded_path}",
            token,
            {
                "message": f"Delete {file.path}",
                "sha": file.sha,
                "branch": branch,
            },
        )
        return result.get("commit", {}).get("sha", "")

    def upload_files(
        self,
        token: str,
        repo: Repository,
        files: Iterable[tuple[str, Path]],
        message: str,
        progress: ProgressCallback | None = None,
    ) -> str:
        selected = list(files)
        if not selected:
            raise GitHubError("Select at least one file to upload.")
        for relative, local_path in selected:
            validate_repository_path(relative)
            details = local_path.lstat()
            if not stat.S_ISREG(details.st_mode):
                raise GitHubError(f"Not a regular file: {relative}")
            if details.st_size > MAX_FILE_SIZE:
                raise GitHubError(f"{relative} is larger than the 100 MB per-file limit.")

        branch = repo.default_branch or "main"
        branch_path = urllib.parse.quote(branch, safe="/-._~")
        reference = None
        try:
            reference = self._request(
                "GET",
                f"repos/{self._repo_path(repo)}/git/ref/heads/{branch_path}",
                token,
            )
        except GitHubError as error:
            if error.status not in (404, 409):
                raise

        if reference is None:
            relative, local_path = selected[0]
            data = _read_regular_file(local_path, relative)
            encoded_path = "/".join(
                urllib.parse.quote(part, safe="-._~")
                for part in validate_repository_path(relative)
            )
            initialized = self._request(
                "PUT",
                f"repos/{self._repo_path(repo)}/contents/{encoded_path}",
                token,
                {
                    "message": f"Initial upload: {message}",
                    "content": base64.b64encode(data).decode("ascii"),
                    "branch": branch,
                },
            )
            parent_sha = initialized.get("commit", {}).get("sha")
            if not parent_sha:
                raise GitHubError("GitHub did not return the initial commit.")
            if progress:
                progress(f"Created initial commit with {relative}.")
            selected = selected[1:]
            if not selected:
                return parent_sha
            parent_commit = self._request(
                "GET",
                f"repos/{self._repo_path(repo)}/git/commits/{urllib.parse.quote(parent_sha, safe='')}",
                token,
            )
            parent_tree = parent_commit.get("tree", {}).get("sha")
        else:
            parent_sha = reference.get("object", {}).get("sha")
            if not parent_sha:
                raise GitHubError("GitHub returned an invalid branch reference.")
            parent_commit = self._request(
                "GET",
                f"repos/{self._repo_path(repo)}/git/commits/{urllib.parse.quote(parent_sha, safe='')}",
                token,
            )
            parent_tree = parent_commit.get("tree", {}).get("sha")

        if not parent_tree:
            raise GitHubError("GitHub returned an invalid parent tree.")
        entries = []
        total = len(selected)
        for index, (relative, local_path) in enumerate(selected, 1):
            data = _read_regular_file(local_path, relative)
            blob = self._request(
                "POST",
                f"repos/{self._repo_path(repo)}/git/blobs",
                token,
                {
                    "content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                },
            )
            entries.append(
                {
                    "path": "/".join(validate_repository_path(relative)),
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
            if progress:
                progress(f"Uploaded file {index}/{total}: {relative}")

        tree = self._request(
            "POST",
            f"repos/{self._repo_path(repo)}/git/trees",
            token,
            {"base_tree": parent_tree, "tree": entries},
        )
        commit = self._request(
            "POST",
            f"repos/{self._repo_path(repo)}/git/commits",
            token,
            {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
        )
        commit_sha = commit.get("sha")
        if not commit_sha:
            raise GitHubError("GitHub did not return the commit.")
        self._request(
            "PATCH",
            f"repos/{self._repo_path(repo)}/git/refs/heads/{branch_path}",
            token,
            {"sha": commit_sha, "force": False},
        )
        if progress:
            progress(f"Published commit {commit_sha[:7]} to {repo.full_name}.")
        return commit_sha

    def download_repository(
        self,
        token: str,
        repo: Repository,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> int:
        files = self.list_files(token, repo, repo.default_branch or "main")
        if len(files) == 0:
            return 0
        root = destination.resolve(strict=True)
        if not root.is_dir():
            raise GitHubError("Choose an existing destination folder.")
        for index, file in enumerate(files, 1):
            if file.size is not None and file.size > MAX_FILE_SIZE:
                raise GitHubError(f"{file.path} is larger than the 100 MB per-file limit.")
            blob = self._request(
                "GET",
                f"repos/{self._repo_path(repo)}/git/blobs/{urllib.parse.quote(file.sha, safe='')}",
                token,
            )
            if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
                raise GitHubError(f"GitHub returned invalid file content for {file.path}.")
            try:
                content = base64.b64decode(blob["content"], validate=False)
            except ValueError:
                raise GitHubError(f"GitHub returned invalid file content for {file.path}.") from None
            if len(content) > MAX_FILE_SIZE:
                raise GitHubError(f"{file.path} is larger than the 100 MB per-file limit.")
            self._write_downloaded_file(root, file.path, content)
            if progress:
                progress(f"Downloaded {index}/{len(files)}: {file.path}")
        return len(files)

    @staticmethod
    def _write_downloaded_file(root: Path, relative_path: str, data: bytes) -> None:
        parts = validate_repository_path(relative_path)
        directory = root
        try:
            for part in parts[:-1]:
                directory = directory / part
                try:
                    details = directory.lstat()
                except FileNotFoundError:
                    directory.mkdir()
                    details = directory.lstat()
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                    raise GitHubError(f"Unsafe destination path for {relative_path}.")

            target = directory / parts[-1]
            try:
                details = target.lstat()
            except FileNotFoundError:
                details = None
            if details is not None and (
                stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)
            ):
                raise GitHubError(f"Unsafe destination file for {relative_path}.")
            if not target.resolve(strict=False).is_relative_to(root):
                raise GitHubError(f"Unsafe destination path for {relative_path}.")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".github-manager-", dir=directory
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, target)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
        except GitHubError:
            raise
        except OSError as error:
            raise GitHubError(f"Could not write {relative_path}: {error}") from None

    @staticmethod
    def _repo_path(repo: Repository) -> str:
        owner, name = repo.full_name.split("/", 1)
        return f"{urllib.parse.quote(owner, safe='-._~')}/{urllib.parse.quote(name, safe='-._~')}"


def authenticate_github(
    api: GitHubAPI,
    client_id: str,
    save_token: Callable[[str], None],
    *,
    progress: Callable[[tuple[str, object]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, dict, list[Repository], GitHubError | None]:
    authorization = api.request_device_authorization(client_id)
    if progress:
        progress(("device", authorization))
    token = api.poll_device_authorization(
        authorization,
        client_id,
        progress=lambda message: progress(("status", message)) if progress else None,
        cancelled=cancelled,
    )
    user = api.get_user(token)
    save_token(token)
    try:
        repositories = api.list_repositories(token)
    except GitHubError as error:
        return token, user, [], error
    return token, user, repositories, None
