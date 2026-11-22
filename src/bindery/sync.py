"""Encrypted, file-oriented synchronisation for Bindery vaults.

The vault is deliberately not a Git working tree.  Git is used as a small,
content-addressed transport for ciphertext, while the local state directory
holds the key, the last agreed manifest, and the private transaction record.
Nothing in this module writes a note without first taking the same vault lock
used by the MCP write path.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import shutil
import signal
import re
import shlex
import socket
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .indexer import is_indexable, refresh_embeddings, index_path, reindex
from .redact import contains_secret
from .safeio import LockTimeout, atomic_write, file_lock
from .store import Store


FORMAT_VERSION = 1
FORMAT_NAME = "AES-256-GCM/HKDF-SHA256"
MAGIC = b"BDY1"
AAD_VERSION = b"bindery-sync/v1"
PUBLIC_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}
DEFAULT_BRANCH = "main"
MAX_GIT_TIMEOUT = 30.0


def _network_timeout() -> float:
    """Seconds allowed for one fetch, push or ls-remote.

    Local git commands finish in well under the 30-second budget, but the
    first fetch of a vault of a few hundred notes moves tens of megabytes of
    ciphertext, which over a VPN can take minutes. A timeout here is safe to
    retry (push timeouts are reconciled by a fetch), so the budget only has
    to be generous, not exact. Override with BINDERY_SYNC_NETWORK_TIMEOUT.
    """
    raw = os.environ.get("BINDERY_SYNC_NETWORK_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else 600.0
    except ValueError:
        value = 600.0
    return value if value > 0 else 600.0
MAX_SYNC_ATTEMPTS = 3
KEY_STORES = {"auto", "keychain", "file"}


class SyncError(RuntimeError):
    """A recoverable sync error."""

    exit_code = 1


class SyncBlocked(SyncError):
    """A condition that needs a human decision before continuing."""

    exit_code = 2


class PlanChanged(SyncError):
    """The vault changed while a persisted application plan was running."""


class SyncTimeout(SyncError):
    """A Git operation timed out and its remote side effect is unknown."""


class KeychainInteractionError(SyncError):
    """macOS Keychain needs an interactive GUI session."""


@dataclass(frozen=True, slots=True)
class Remote:
    value: str
    kind: str
    path: Path | None = None
    host: str | None = None
    display: str | None = None


@dataclass(frozen=True, slots=True)
class SyncPaths:
    root: Path
    repo: Path
    state: Path
    key: Path
    format: Path
    transaction: Path


@dataclass(slots=True)
class SyncResult:
    exit_code: int
    status: dict
    message: str = ""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _short_error(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _key_store_choice(value: str | None, *, use_environment: bool = True) -> str:
    raw = value
    if raw is None and use_environment:
        raw = os.environ.get("BINDERY_KEY_STORE")
    choice = str(raw or "auto").strip().lower()
    if choice not in KEY_STORES:
        raise SyncError("key store must be one of: auto, keychain, file")
    return choice


def _keychain_interaction_error(value: object) -> bool:
    detail = _short_error(value).lower()
    return "-25308" in detail or "user interaction is not allowed" in detail or "interaction is not allowed" in detail


def _error_without_secrets(value: object, secrets: tuple[bytes, ...] = ()) -> str:
    message = _short_error(value)
    for secret in secrets:
        if secret:
            message = message.replace(base64.b64encode(secret).decode("ascii"), "[redacted]")
    return message


def _safe_slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:48] or "machine"


def vault_key(vault: Path) -> str:
    return hashlib.sha256(str(vault.resolve()).encode("utf-8")).hexdigest()[:32]


def paths_for(config) -> SyncPaths:
    root = Path(config.state_dir) / "sync" / vault_key(config.vault)
    return SyncPaths(
        root=root,
        repo=root / "repo",
        state=root / "state.json",
        key=root / "master.key",
        format=root / "repo" / "format.json",
        transaction=root / "transaction.json",
    )


def _read_json(path: Path, default: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return dict(default or {})
    return value if isinstance(value, dict) else dict(default or {})


def _write_json(path: Path, value: dict, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    try:
        path.chmod(mode)
    except OSError:
        pass


def validate_remote(value: str, *, allow_public_host: bool = False) -> Remote:
    """Accept only the transports that the sync design explicitly permits."""
    raw = str(value or "").strip()
    if not raw or raw.startswith("-") or "\x00" in raw:
        raise SyncError("remote must be a non-empty safe URL")
    if raw.startswith("ext::") or raw.startswith("git://") or raw.startswith("http://") or raw.startswith("https://"):
        raise SyncError("remote transport is not allowed; use ssh://, scp, file://, or an absolute path")

    def normalise_host(host: str) -> str:
        host = host.rstrip(".").lower()
        if not host or host.startswith("-"):
            raise SyncError("remote host is invalid")
        return host

    def public_host(host: str) -> None:
        if host in PUBLIC_HOSTS and not allow_public_host:
            raise SyncError(f"public host {host} is refused; pass --allow-public-host explicitly")

    if raw.startswith("file://"):
        parsed = urlparse(raw)
        if parsed.netloc or parsed.username or parsed.password:
            raise SyncError("file://host remotes are not allowed")
        path = Path(unquote(parsed.path))
        if not path.is_absolute():
            raise SyncError("file:// remote path must be absolute")
        return Remote(raw, "file", path=path, display=raw)

    if raw.startswith("ssh://"):
        parsed = urlparse(raw)
        if not parsed.hostname or parsed.password is not None:
            raise SyncError("ssh remote must have a host and no password")
        host = normalise_host(parsed.hostname)
        public_host(host)
        try:
            port = parsed.port
        except ValueError as exc:
            raise SyncError("ssh remote port is invalid") from exc
        user = parsed.username
        if user is not None and not re.fullmatch(r"[A-Za-z0-9._-]+", user):
            raise SyncError("ssh remote username is invalid")
        userinfo = f"{user}@" if user else ""
        normalized = f"ssh://{userinfo}{host}{':' + str(port) if port else ''}{parsed.path}"
        display_user = "***@" if user else ""
        display = f"ssh://{display_user}{host}{':' + str(port) if port else ''}{parsed.path}"
        return Remote(normalized, "ssh", host=host, display=display)

    # scp syntax.  A colon is required, but a Windows drive is not an
    # accepted remote on this Unix-oriented CLI.
    match = re.fullmatch(r"(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[^:/\s]+):(?P<path>.+)", raw)
    if match:
        host = normalise_host(match.group("host"))
        public_host(host)
        user = match.group("user")
        normalized = f"{user + '@' if user else ''}{host}:{match.group('path')}"
        display = f"{'***@' if user else ''}{host}:{match.group('path')}"
        return Remote(normalized, "scp", host=host, display=display)

    path = Path(raw).expanduser()
    if path.is_absolute():
        return Remote(str(path), "path", path=path, display=str(path))
    raise SyncError("remote must use ssh://, scp syntax, file://, or an absolute path")


def _remote_for_git(remote: Remote) -> str:
    return str(remote.path) if remote.kind in {"file", "path"} and remote.path else remote.value


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _git_options(paths: SyncPaths) -> list[str]:
    hooks = paths.root / "hooks-disabled"
    hooks.mkdir(parents=True, exist_ok=True)
    return [
        "-c", "protocol.allow=never",
        "-c", "protocol.ssh.allow=always",
        "-c", "protocol.file.allow=always",
        "-c", f"core.hooksPath={hooks}",
        "-c", "core.symlinks=false",
        "-c", "core.excludesFile=",
    ]


def _run_killing_group(command: list[str], *, input_data: bytes | None, env: dict[str, str],
                       timeout: float) -> subprocess.CompletedProcess:
    """Run ``command`` in its own process group and kill the whole group on timeout.

    ``subprocess.run(timeout=...)`` kills only git itself. A fetch or push
    runs ssh as a child, and that ssh survives git, keeps the connection
    open, and leaves git-upload-pack / index-pack waiting on the far side
    indefinitely. Killing the group ends the transport as well.
    """
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _git(paths: SyncPaths, *args: str, input_data: bytes | None = None, timeout: float = MAX_GIT_TIMEOUT,
         check: bool = True) -> subprocess.CompletedProcess:
    command = ["git", *(_git_options(paths)), "-C", str(paths.repo), *args]
    try:
        result = _run_killing_group(command, input_data=input_data, env=_git_env(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SyncTimeout(f"git operation timed out: {_short_error(exc)}") from exc
    except OSError as exc:
        raise SyncError(f"git operation failed: {_short_error(exc)}") from exc
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace")
        raise SyncError(f"git operation failed: {_short_error(detail)}")
    return result


def _git_text(paths: SyncPaths, *args: str, check: bool = True) -> str:
    return _git(paths, *args, check=check).stdout.decode("utf-8", "replace").strip()


def _git_show(paths: SyncPaths, revision: str, name: str) -> bytes:
    result = _git(paths, "show", f"{revision}:{name}", check=False)
    if result.returncode:
        return b""
    return result.stdout


def _git_head(paths: SyncPaths, revision: str = "HEAD") -> str | None:
    value = _git_text(paths, "rev-parse", "--verify", revision, check=False)
    return value or None


def _initialise_repo(paths: SyncPaths, remote: Remote, *, write: bool) -> None:
    if paths.repo.exists() and (paths.repo / ".git").is_dir():
        return
    if not write:
        return
    paths.repo.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "init", "--initial-branch", DEFAULT_BRANCH, str(paths.repo)],
        capture_output=True, text=True, env=_git_env(), check=False,
    )
    if result.returncode:
        raise SyncError(_short_error(result.stderr))
    if remote.kind in {"file", "path"} and remote.path is not None and not (remote.path / "HEAD").exists():
        remote.path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "init", "--bare", str(remote.path)],
            capture_output=True, text=True, env=_git_env(), check=False,
        )
        if result.returncode:
            raise SyncError(_short_error(result.stderr))
    existing = _git_text(paths, "remote", "get-url", "bindery", check=False)
    if existing and existing != _remote_for_git(remote):
        raise SyncBlocked("remote name bindery is already configured for another destination")
    if not existing:
        _git(paths, "remote", "add", "bindery", _remote_for_git(remote))


def _point_repo_at(paths: SyncPaths, remote: Remote) -> None:
    """Keep the private sync repository's remote equal to the validated state.

    The repository under the state directory belongs to Bindery alone, and the
    state file is the record of where this machine syncs to. A re-pair with a
    different spelling of the remote updates the state, so the repository
    follows it rather than silently pushing to the old destination.
    """
    wanted = _remote_for_git(remote)
    existing = _git_text(paths, "remote", "get-url", "bindery", check=False)
    if not existing:
        _git(paths, "remote", "add", "bindery", wanted)
    elif existing != wanted:
        _git(paths, "remote", "set-url", "bindery", wanted)


def _ensure_format(paths: SyncPaths, *, write: bool) -> None:
    expected = {"version": FORMAT_VERSION, "encryption": FORMAT_NAME}
    if paths.format.exists():
        current = _read_json(paths.format)
        if current != expected:
            raise SyncBlocked("encrypted repository format is incompatible")
        return
    if write:
        _write_json(paths.format, expected)


def _load_state(paths: SyncPaths) -> dict:
    state = _read_json(paths.state)
    state.setdefault("configured", False)
    state.setdefault("git", "never")
    state.setdefault("index", "never")
    state.setdefault("held_secrets", [])
    state.setdefault("conflicts", 0)
    state.setdefault("peers", [])
    state.setdefault("encryption_counts", {})
    state.setdefault("remote_display", state.get("remote"))
    return state


def _save_state(paths: SyncPaths, state: dict) -> None:
    _write_json(paths.state, state)


def _machine_id(config) -> str:
    path = Path(config.state_dir) / "machine-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    override = os.environ.get("BINDERY_HOST", "").strip()
    label = _safe_slug(override or socket.gethostname().split(".", 1)[0])
    value = f"{label}-{os.urandom(3).hex()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, value + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def machine_label(config) -> str:
    override = os.environ.get("BINDERY_HOST", "").strip()
    if override:
        return _safe_slug(override)
    return _machine_id(config)


def sync_is_configured(config) -> bool:
    return _load_state(paths_for(config)).get("configured", False)


def sync_marker(config) -> tuple[int, str] | None:
    state = _load_state(paths_for(config))
    if not state.get("configured"):
        return None
    return int(state.get("applied_generation", 0)), str(state.get("applied_manifest", ""))


def _keychain_account(sync_id: str) -> str:
    return sync_id


def _ensure_private_key_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _key_file_write(path: Path, key: bytes) -> None:
    _ensure_private_key_dir(path.parent)
    atomic_write(path, base64.b64encode(key).decode("ascii") + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _keychain_get(sync_id: str) -> bytes | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "bindery-sync", "-a", _keychain_account(sync_id), "-w"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        return base64.b64decode(str(getattr(result, "stdout", "")).strip(), validate=True)
    except (ValueError, base64.binascii.Error):
        return None


def _keychain_put(sync_id: str, key: bytes, *, replace: bool = False) -> None:
    if sys.platform != "darwin":
        raise SyncError("Keychain storage is only available on macOS")
    encoded = base64.b64encode(key).decode("ascii")
    # The secret is sent over stdin, never in argv or a temporary file.
    command = f"add-generic-password -s bindery-sync -a {shlex.quote(_keychain_account(sync_id))} -w {shlex.quote(encoded)}"
    if replace:
        command += " -U"
    command += "\n"
    try:
        result = subprocess.run(
            ["/usr/bin/security", "-i"], input=command, text=True,
            capture_output=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SyncError(f"Keychain update failed: {_short_error(exc)}") from exc
    if result.returncode:
        detail = _short_error(f"{getattr(result, 'stderr', '')} {getattr(result, 'stdout', '')}")
        message = f"Keychain update failed: {detail}"
        if _keychain_interaction_error(f"{result.returncode} {message}"):
            raise KeychainInteractionError(message)
        raise SyncError(message)


def _state_key_store(state: dict, *, requested: str | None = None) -> str:
    recorded = state.get("key_store")
    if recorded in {"keychain", "file"}:
        return str(recorded)
    choice = _key_store_choice(requested)
    if choice == "auto":
        return "keychain" if sys.platform == "darwin" else "file"
    return choice


def _read_file_key(path: Path) -> bytes | None:
    try:
        encoded = path.read_text(encoding="ascii").strip()
        key = base64.b64decode(encoded, validate=True)
    except (OSError, UnicodeError, ValueError, base64.binascii.Error):
        return None
    return key if len(key) == 32 else None


def load_key(config, state: dict, paths: SyncPaths, *, key_store: str | None = None) -> bytes:
    sync_id = str(state.get("sync_id", ""))
    if not sync_id:
        raise SyncBlocked("sync is not paired; run sync init or sync pair")
    selected = _state_key_store(state, requested=key_store)
    if selected == "keychain":
        key = _keychain_get(sync_id)
    else:
        key = _read_file_key(paths.key)
    if key is None or len(key) != 32:
        raise SyncBlocked("sync key is unavailable; pair again or restore it from a password manager")
    if state.get("key_store") not in {"keychain", "file"}:
        state["key_store"] = selected
    return key


def store_key(config, state: dict, paths: SyncPaths, key: bytes, *, replace: bool = False,
              key_store: str | None = None) -> None:
    if len(key) != 32:
        raise SyncError("sync key must be 32 bytes")
    if key_store is not None:
        selected = _key_store_choice(key_store)
    elif state.get("key_store") in {"keychain", "file"}:
        selected = str(state["key_store"])
    else:
        selected = _key_store_choice(None)
    if selected == "auto":
        if sys.platform != "darwin":
            selected = "file"
        else:
            try:
                _keychain_put(str(state["sync_id"]), key, replace=replace)
                selected = "keychain"
            except (KeychainInteractionError, SyncError) as exc:
                if not _keychain_interaction_error(exc):
                    raise
                _key_file_write(paths.key, key)
                selected = "file"
            state["key_store"] = selected
            return
    if selected == "keychain":
        _keychain_put(str(state["sync_id"]), key, replace=replace)
    else:
        _ensure_private_key_dir(paths.key.parent)
        if paths.key.exists() and not replace:
            existing = _read_file_key(paths.key)
            if existing is None:
                raise SyncBlocked("sync key file is unavailable; pair again or restore it from a password manager")
            if not hmac.compare_digest(existing, key):
                raise SyncBlocked("a different key is already registered for this sync id")
            state["key_store"] = "file"
            return
        _key_file_write(paths.key, key)
    state["key_store"] = selected


def derive_keys(master: bytes) -> tuple[bytes, bytes, bytes]:
    if len(master) != 32:
        raise ValueError("master key must be 32 bytes")
    def derive(info: bytes, length: int) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master)
    return derive(b"bindery-sync/enc", 32), derive(b"bindery-sync/name", 32), derive(b"bindery-sync/key-id", 8)


def object_id(name_key: bytes, rel: str) -> str:
    return hmac.new(name_key, rel.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _aad(*parts: object) -> bytes:
    encoded: list[bytes] = []
    for part in parts:
        value = part if isinstance(part, bytes) else str(part).encode("utf-8")
        encoded.append(len(value).to_bytes(4, "big") + value)
    return b"".join(encoded)


def encrypt_object(master: bytes, sync_id: str, obj_id: str, plaintext: bytes) -> bytes:
    enc_key, _name_key, key_id = derive_keys(master)
    header = MAGIC + key_id
    nonce = os.urandom(12)
    aad = _aad(AAD_VERSION, b"object", sync_id, obj_id)
    return header + nonce + AESGCM(enc_key).encrypt(nonce, plaintext, aad)


def decrypt_object(master: bytes, sync_id: str, obj_id: str, blob: bytes) -> bytes:
    enc_key, _name_key, key_id = derive_keys(master)
    if len(blob) < 4 + 8 + 12 + 16 or blob[:4] != MAGIC or blob[4:12] != key_id:
        raise SyncBlocked("encrypted object header is invalid")
    nonce = blob[12:24]
    aad = _aad(AAD_VERSION, b"object", sync_id, obj_id)
    try:
        return AESGCM(enc_key).decrypt(nonce, blob[24:], aad)
    except Exception as exc:
        raise SyncBlocked("encrypted object authentication failed") from exc


def encrypt_manifest(master: bytes, sync_id: str, generation: int, plaintext: bytes) -> bytes:
    enc_key, _name_key, key_id = derive_keys(master)
    header = MAGIC + key_id + int(generation).to_bytes(8, "big")
    nonce = os.urandom(12)
    aad = _aad(AAD_VERSION, header, b"manifest", sync_id, b"manifest", generation)
    return header + nonce + AESGCM(enc_key).encrypt(nonce, plaintext, aad)


def decrypt_manifest(master: bytes, sync_id: str, blob: bytes) -> tuple[int, bytes]:
    enc_key, _name_key, key_id = derive_keys(master)
    if len(blob) < 4 + 8 + 8 + 12 + 16 or blob[:4] != MAGIC or blob[4:12] != key_id:
        raise SyncBlocked("encrypted manifest header is invalid")
    generation = int.from_bytes(blob[12:20], "big")
    nonce = blob[20:32]
    header = blob[:20]
    aad = _aad(AAD_VERSION, header, b"manifest", sync_id, b"manifest", generation)
    try:
        plaintext = AESGCM(enc_key).decrypt(nonce, blob[32:], aad)
    except Exception as exc:
        raise SyncBlocked("encrypted manifest authentication failed") from exc
    try:
        document = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SyncBlocked("decrypted manifest is not valid UTF-8 JSON") from exc
    if int(document.get("generation", -1)) != generation:
        raise SyncBlocked("manifest generation does not match its header")
    return generation, plaintext


def _boundary_excluded(rel: str, config) -> bool:
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    if any(part in {".git", ".obsidian", ".trash", ".DS_Store"} for part in parts):
        if rel == ".bindery-sync/machines.json":
            return False
        if ".obsidian" in parts or ".trash" in parts:
            return True
    if any(part.startswith("workspace") for part in parts if ".obsidian" in parts):
        return True
    if rel.endswith(".tmp") or rel == ".DS_Store" or ".DS_Store" in parts:
        return True
    if rel != ".bindery-sync/machines.json" and not is_indexable(rel, include=config.include, exclude=config.exclude):
        return True
    return False


def _safe_rel(rel: str) -> str:
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or "\x00" in rel:
        raise SyncBlocked(f"unsafe vault path: {rel!r}")
    parts = rel.split("/")
    if any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts):
        raise SyncBlocked(f"unsafe vault path: {rel!r}")
    normalized = unicodedata.normalize("NFC", rel)
    if normalized != rel:
        raise SyncBlocked(f"vault path is not NFC-normalized: {rel!r}")
    return rel


def _scan_vault(config, *, held: list[str] | None = None) -> tuple[dict[str, dict], list[str], list[str]]:
    """Return manifest, secret-held paths, and unreadable Markdown paths."""
    manifest: dict[str, dict] = {}
    held_paths: list[str] = []
    unreadable: list[str] = []
    vault = config.vault.resolve()
    if not vault.exists():
        return manifest, held_paths, unreadable
    for path in sorted(vault.rglob("*")):
        if path.is_dir() or path.is_symlink():
            continue
        real = path.resolve()
        try:
            real.relative_to(vault)
        except ValueError:
            continue
        rel = _safe_rel(str(real.relative_to(vault)))
        if _boundary_excluded(rel, config):
            continue
        try:
            data = real.read_bytes()
        except OSError:
            continue
        if rel.lower().endswith(".md"):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                unreadable.append(rel)
                continue
            if contains_secret(text):
                held_paths.append(rel)
                continue
        manifest[rel] = {"sha256": _sha256(data), "size": len(data), "machine": machine_label(config)}
    if held is not None:
        held[:] = sorted(held_paths)
    return manifest, held_paths, unreadable


def _machines_path(config) -> Path:
    return config.vault / ".bindery-sync" / "machines.json"


def _register_machine(config, label: str, *, write: bool) -> None:
    path = _machines_path(config)
    data = _read_json(path, {"machines": {}})
    machines = data.get("machines")
    if not isinstance(machines, dict):
        raise SyncBlocked(".bindery-sync/machines.json is invalid")
    current = _machine_id(config)
    previous = machines.get(label)
    if previous and previous != current:
        raise SyncBlocked(f"machine label {label!r} is already registered to another machine")
    if previous == current:
        return
    if write:
        machines[label] = current
        _write_json(path, {"machines": dict(sorted(machines.items()))}, mode=0o600)


def _manifest_document(files: dict[str, dict], generation: int, previous_hash: str | None) -> dict:
    return {"generation": generation, "previous_hash": previous_hash, "files": files}


def _manifest_hash(document: dict) -> str:
    return _sha256(_json_bytes(document))


def _manifest_entry(manifest: dict, rel: str) -> dict | None:
    value = manifest.get("files", {}).get(rel)
    return value if isinstance(value, dict) else None


def _manifest_files(manifest: dict) -> dict[str, dict]:
    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    return files if isinstance(files, dict) else {}


def _fetch(paths: SyncPaths) -> None:
    result = _git(paths, "fetch", "--prune", "bindery", f"+refs/heads/{DEFAULT_BRANCH}:refs/remotes/bindery/{DEFAULT_BRANCH}",
                  check=False, timeout=_network_timeout())
    if result.returncode:
        # An empty freshly-created bare repository has no main ref yet. That
        # is the only fetch failure we treat as an empty remote; transport and
        # authentication failures remain ordinary errors.
        probe = _git(paths, "ls-remote", "--heads", "bindery", check=False, timeout=_network_timeout())
        if probe.returncode or probe.stdout.strip():
            raise SyncError(_short_error(result.stderr.decode("utf-8", "replace")))


def _remote_head(paths: SyncPaths) -> str | None:
    return _git_head(paths, f"refs/remotes/bindery/{DEFAULT_BRANCH}")


def _read_remote_manifest(paths: SyncPaths, master: bytes, sync_id: str, commit: str | None) -> tuple[dict, str, int] | None:
    if not commit:
        return None
    blob = _git_show(paths, commit, "manifest.enc")
    if not blob:
        raise SyncBlocked("remote manifest is missing")
    generation, plaintext = decrypt_manifest(master, sync_id, blob)
    try:
        document = json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SyncBlocked("remote manifest JSON is invalid") from exc
    if not isinstance(document, dict) or not isinstance(document.get("files"), dict):
        raise SyncBlocked("remote manifest structure is invalid")
    return document, _sha256(plaintext), generation


def _manifest_candidates(paths: SyncPaths, master: bytes, sync_id: str, commit: str | None) -> dict[str, tuple[dict, int]]:
    """Index authenticated manifests reachable from a remote ref.

    Git is only used to find encrypted manifest blobs. The acceptance decision
    below follows the authenticated ``previous_hash`` values and generations;
    Git parent relationships are deliberately not part of that decision.
    """
    if not commit:
        return {}
    candidates: dict[str, tuple[dict, int]] = {}
    commits = _git_text(paths, "rev-list", commit, check=False).splitlines()
    for item in commits:
        blob = _git_show(paths, item, "manifest.enc")
        if not blob:
            continue
        try:
            generation, plaintext = decrypt_manifest(master, sync_id, blob)
            document = json.loads(plaintext.decode("utf-8"))
        except (SyncBlocked, UnicodeDecodeError, ValueError, TypeError):
            continue
        if not isinstance(document, dict) or not isinstance(document.get("files"), dict):
            continue
        candidates[_sha256(plaintext)] = (document, generation)
    return candidates


def _read_object_at(paths: SyncPaths, master: bytes, sync_id: str, commit: str, obj: str) -> bytes:
    blob = _git_show(paths, commit, f"objects/{obj}.enc")
    if not blob:
        raise SyncBlocked(f"encrypted object {obj} is missing")
    return decrypt_object(master, sync_id, obj, blob)


def _read_local_object(paths: SyncPaths, master: bytes, sync_id: str, obj: str) -> bytes:
    path = paths.repo / "objects" / f"{obj}.enc"
    if not path.exists():
        commit = _git_head(paths)
        if commit:
            return _read_object_at(paths, master, sync_id, commit, obj)
        raise SyncBlocked(f"encrypted object {obj} is missing")
    return decrypt_object(master, sync_id, obj, path.read_bytes())


def _content_for_entry(paths: SyncPaths, master: bytes, sync_id: str, entry: dict | None, *, commit: str | None = None) -> bytes | None:
    if not entry:
        return None
    obj = str(entry.get("object", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", obj):
        raise SyncBlocked("manifest contains an invalid object id")
    data = _read_object_at(paths, master, sync_id, commit, obj) if commit else _read_local_object(paths, master, sync_id, obj)
    if _sha256(data) != entry.get("sha256") or len(data) != int(entry.get("size", -1)):
        raise SyncBlocked("manifest object hash or size does not match")
    return data


def _conflict_path(rel: str, sha: str) -> str:
    path = Path(rel)
    return str(path.with_name(f"{path.stem}.conflict-{sha[:12]}{path.suffix}"))


def _changed(entry: dict | None, base: dict | None) -> bool:
    if entry is None and base is None:
        return False
    if entry is None or base is None:
        return entry != base
    return entry.get("sha256") != base.get("sha256")


@dataclass(slots=True)
class PlanItem:
    path: str
    data: bytes | None
    expected_sha: str | None
    delete: bool = False
    source: str = "local"
    object_id: str | None = None


@dataclass(slots=True)
class SyncPlan:
    items: list[PlanItem] = field(default_factory=list)
    publish: dict[str, dict] = field(default_factory=dict)
    base_after: dict[str, dict] = field(default_factory=dict)
    conflicts: int = 0
    conflict_paths: list[str] = field(default_factory=list)
    remote_secret_paths: list[str] = field(default_factory=list)


def _conflict_entry(data: bytes, template: dict | None = None) -> dict:
    """Build a manifest entry for a deterministic conflict copy."""
    entry = {
        "sha256": _sha256(data),
        "size": len(data),
        "machine": str((template or {}).get("machine", "")),
    }
    return entry


def _reserve_plan_version(config, plan: SyncPlan, path: str, data: bytes, template: dict | None = None,
                          *, source: str = "remote", seen: set[tuple[str, str]] | None = None) -> None:
    """Reserve ``data`` at ``path`` without overwriting an existing version.

    A conflict copy can itself be the target of a normal three-way update in
    the same plan.  If that would overwrite the version currently at the
    deterministic path, retain the current bytes under another deterministic
    hash-derived name before installing the requested version.  The recursion
    is only for pre-existing nested conflict copies and is bounded by the
    path/content pair set.
    """
    data_sha = _sha256(data)
    seen = seen if seen is not None else set()
    marker = (path, data_sha)
    if marker in seen:
        return
    seen.add(marker)

    existing_entry = plan.publish.get(path)
    if existing_entry is not None and existing_entry.get("sha256") != data_sha:
        raise SyncBlocked(f"conflicting versions claim the same conflict path: {path}")

    target = config.vault / path
    current_sha = _current_sha(target)
    if target.exists() and not target.is_file():
        raise SyncBlocked(f"conflict copy is not a regular file: {path}")
    if current_sha and current_sha != data_sha:
        current_data = target.read_bytes()
        current_entry = _conflict_entry(current_data)
        nested = _conflict_path(path, current_sha)
        _reserve_plan_version(config, plan, nested, current_data, current_entry, source="local", seen=seen)

    for item in plan.items:
        if item.path != path:
            continue
        planned_sha = None if item.delete or item.data is None else _sha256(item.data)
        if item.delete or planned_sha != data_sha:
            raise SyncBlocked(f"conflicting writes claim the same conflict path: {path}")
        break
    else:
        if current_sha != data_sha:
            plan.items.append(PlanItem(path, data, current_sha, source=source))

    entry = _conflict_entry(data, template)
    plan.publish[path] = entry
    plan.base_after[path] = dict(entry)


def _plan_claimed_conflict_copy(config, plan: SyncPlan, path: str, claim_data: bytes, claim_entry: dict,
                                local: dict | None, remote: dict | None,
                                local_data: bytes | None, remote_data: bytes | None) -> None:
    """Keep a primary conflict's loser at its deterministic copy path.

    The copy path is also a normal manifest path.  Any local or remote version
    that would otherwise be updated at that path is therefore preserved as a
    second deterministic conflict copy instead of being overwritten.
    """
    _reserve_plan_version(config, plan, path, claim_data, claim_entry,
                          source="remote" if remote and remote.get("sha256") == _sha256(claim_data) else "local")
    seen_hashes = {_sha256(claim_data)}
    for entry, data, source in (
        (local, local_data, "local"),
        (remote, remote_data, "remote"),
    ):
        if data is None:
            continue
        data_sha = _sha256(data)
        if data_sha in seen_hashes:
            continue
        seen_hashes.add(data_sha)
        alias = _conflict_path(path, data_sha)
        _reserve_plan_version(config, plan, alias, data, entry, source=source)


def _plan(config, paths: SyncPaths, master: bytes, sync_id: str, base_doc: dict, local_files: dict,
          remote_doc: dict | None, remote_commit: str | None, held: set[str]) -> SyncPlan:
    base_files = _manifest_files(base_doc)
    remote_files = _manifest_files(remote_doc or {})
    plan = SyncPlan()
    all_paths = sorted(set(base_files) | set(local_files) | set(remote_files))
    local_content: dict[str, bytes | None] = {}
    remote_content: dict[str, bytes | None] = {}
    conflict_claims: dict[str, tuple[bytes, dict]] = {}
    for rel in all_paths:
        if rel in local_files:
            local_content[rel] = (config.vault / rel).read_bytes()
        if rel in remote_files:
            remote_content[rel] = _content_for_entry(paths, master, sync_id, remote_files[rel], commit=remote_commit)
            if rel.lower().endswith(".md") and remote_content[rel] is not None:
                try:
                    if contains_secret(remote_content[rel].decode("utf-8")):
                        plan.remote_secret_paths.append(rel)
                except UnicodeDecodeError:
                    pass

    # Collect claims before walking ``all_paths``.  Generated conflict names
    # sort before their primary path (``note.conflict-...`` comes before
    # ``note.md``), so discovering claims inline would let the ordinary update
    # of the copy run first and overwrite the version the primary conflict is
    # about to preserve.
    for rel in all_paths:
        base = base_files.get(rel)
        local = local_files.get(rel)
        remote = remote_files.get(rel)
        if rel in held:
            if remote and _changed(remote, base) and remote.get("sha256") != (local or {}).get("sha256") and rel.lower().endswith(".md"):
                conflict = _conflict_path(rel, str(remote["sha256"]))
                conflict_claims[conflict] = (remote_content[rel], dict(remote))
            continue
        if rel == ".bindery-sync/machines.json" or not (local and remote and _changed(local, base) and _changed(remote, base)):
            continue
        if not rel.lower().endswith(".md"):
            continue
        winner, loser = (local, remote) if local["sha256"] > remote["sha256"] else (remote, local)
        loser_data = local_content[rel] if loser is local else remote_content[rel]
        conflict = _conflict_path(rel, str(loser["sha256"]))
        existing_claim = conflict_claims.get(conflict)
        if existing_claim is not None and _sha256(existing_claim[0]) != _sha256(loser_data):
            raise SyncBlocked(f"conflicting versions claim the same conflict path: {conflict}")
        conflict_claims[conflict] = (loser_data, dict(loser))

    for rel in all_paths:
        if rel != ".bindery-sync/machines.json" and _boundary_excluded(rel, config):
            # A path excluded on this machine is neither deleted locally nor
            # silently removed from the shared manifest. The boundary is a
            # read/write filter, not a deletion command.
            preserved = remote_files.get(rel) or base_files.get(rel)
            if preserved:
                plan.publish[rel] = dict(preserved)
                plan.base_after[rel] = dict(preserved)
            continue
        target = config.vault / rel
        if target.is_symlink():
            try:
                target.resolve().relative_to(config.vault.resolve())
            except ValueError:
                preserved = remote_files.get(rel) or base_files.get(rel)
                if preserved:
                    plan.publish[rel] = dict(preserved)
                    plan.base_after[rel] = dict(preserved)
                continue
        base = base_files.get(rel)
        local = local_files.get(rel)
        remote = remote_files.get(rel)
        claim = conflict_claims.get(rel)
        if claim is not None:
            _plan_claimed_conflict_copy(
                config,
                plan,
                rel,
                claim[0],
                claim[1],
                local,
                remote,
                local_content.get(rel),
                remote_content.get(rel),
            )
            continue
        if rel in held:
            if base:
                plan.publish[rel] = dict(base)
                plan.base_after[rel] = dict(base)
            # Keep the held local value in place. A changed remote version is
            # preserved as a deterministic conflict copy, never promoted to
            # the secret-bearing primary path.
            if remote and _changed(remote, base) and remote.get("sha256") != (local or {}).get("sha256"):
                if rel.lower().endswith(".md"):
                    conflict = _conflict_path(rel, str(remote.get("sha256", "")))
                    conflict_target = config.vault / conflict
                    conflict_sha = _current_sha(conflict_target)
                    if conflict_sha and conflict_sha != remote["sha256"]:
                        raise SyncBlocked(f"conflict copy already contains different content: {conflict}")
                    if conflict_sha != remote["sha256"]:
                        plan.items.append(PlanItem(conflict, remote_content[rel], conflict_sha, source="remote"))
                    plan.conflicts += 1
                    plan.conflict_paths.append(conflict)
                else:
                    raise SyncBlocked(f"secret-held non-Markdown conflict: {rel}")
            continue

        local_changed = _changed(local, base)
        remote_changed = _changed(remote, base)
        if local and remote and local.get("sha256") == remote.get("sha256"):
            chosen, content = local, local_content[rel]
            plan.publish[rel] = dict(chosen)
            plan.base_after[rel] = dict(chosen)
            if (config.vault / rel).exists() and _sha256((config.vault / rel).read_bytes()) != chosen["sha256"]:
                plan.items.append(PlanItem(rel, content, chosen.get("sha256")))
            continue
        if local_changed and remote_changed and local and remote:
            if rel == ".bindery-sync/machines.json":
                try:
                    local_machines = json.loads(local_content[rel].decode("utf-8")).get("machines", {})
                    remote_machines = json.loads(remote_content[rel].decode("utf-8")).get("machines", {})
                except (ValueError, UnicodeDecodeError, AttributeError) as exc:
                    raise SyncBlocked("machine registry is invalid") from exc
                if not isinstance(local_machines, dict) or not isinstance(remote_machines, dict):
                    raise SyncBlocked("machine registry is invalid")
                merged = {**remote_machines, **local_machines}
                for label, machine in remote_machines.items():
                    if label in local_machines and local_machines[label] != machine:
                        raise SyncBlocked(f"machine label {label!r} is already registered to another machine")
                merged_data = (_json_bytes({"machines": dict(sorted(merged.items()))}) + b"\n")
                merged_entry = {"sha256": _sha256(merged_data), "size": len(merged_data), "machine": machine_label(config)}
                plan.items.append(PlanItem(rel, merged_data, _sha256(local_content[rel]), source="local"))
                plan.publish[rel] = merged_entry
                plan.base_after[rel] = merged_entry
                continue
            if not rel.lower().endswith(".md"):
                raise SyncBlocked(f"non-Markdown content conflict: {rel}")
            try:
                local_content[rel].decode("utf-8")
                remote_content[rel].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SyncBlocked(f"non-UTF-8 Markdown conflict: {rel}") from exc
            winner, loser = (local, remote) if local["sha256"] > remote["sha256"] else (remote, local)
            winner_data = local_content[rel] if winner is local else remote_content[rel]
            loser_data = local_content[rel] if loser is local else remote_content[rel]
            conflict = _conflict_path(rel, str(loser["sha256"]))
            target = config.vault / conflict
            conflict_claims[conflict] = (loser_data, dict(loser))
            # Write a new conflict copy immediately.  An existing copy is
            # handled when its own manifest path reaches the second pass below
            # so its three-way update can be preserved as another copy.
            if not target.exists():
                _reserve_plan_version(
                    config,
                    plan,
                    conflict,
                    loser_data,
                    loser,
                    source="remote" if loser is remote else "local",
                )
            plan.items.append(PlanItem(
                rel,
                winner_data,
                (local or {}).get("sha256"),
                source="remote" if winner is remote else "local",
            ))
            plan.publish[rel] = dict(winner)
            plan.publish[conflict] = {"sha256": _sha256(loser_data), "size": len(loser_data), "machine": loser.get("machine", "")}
            plan.base_after[rel] = dict(winner)
            plan.base_after[conflict] = dict(plan.publish[conflict])
            plan.conflicts += 1
            plan.conflict_paths.append(conflict)
            continue

        if remote_changed and remote is None and local is not None and not local_changed:
            plan.items.append(PlanItem(rel, None, local.get("sha256"), delete=True, source="remote"))
            plan.base_after.pop(rel, None)
        elif remote_changed and remote is not None:
            plan.items.append(PlanItem(rel, remote_content[rel], (local or {}).get("sha256"), source="remote"))
            plan.publish[rel] = dict(remote)
            plan.base_after[rel] = dict(remote)
        elif local is not None:
            plan.publish[rel] = dict(local)
            plan.base_after[rel] = dict(local)
        elif base is not None:
            plan.items.append(PlanItem(rel, None, (config.vault / rel).exists() and _sha256((config.vault / rel).read_bytes()) or None, delete=True, source="remote"))
            plan.base_after.pop(rel, None)

    _validate_plan_paths(config, plan, all_paths)
    return plan


def _current_sha(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return _sha256(path.read_bytes())
    except OSError:
        return None


def _validate_plan_paths(config, plan: SyncPlan, all_paths: set[str]) -> None:
    """Reject every unsafe or ambiguous path before the first vault write."""
    vault = config.vault.resolve()
    destinations = set(plan.publish) | {item.path for item in plan.items}
    normalized: dict[str, str] = {}
    for rel in set(all_paths) | destinations:
        safe = _safe_rel(rel)
        key = safe.casefold()
        other = normalized.get(key)
        if other is not None:
            raise SyncBlocked(f"case-insensitive path collision: {other}, {safe}")
        normalized[key] = safe

    safe_destinations = sorted({_safe_rel(rel) for rel in destinations})
    destination_set = set(safe_destinations)
    for safe in safe_destinations:
        target = config.vault / safe
        if target.is_symlink():
            raise SyncBlocked(f"refusing to write through symlink: {safe}")
        try:
            target.parent.resolve().relative_to(vault)
        except ValueError as exc:
            raise SyncBlocked(f"vault path escapes through a symlink: {safe}") from exc
        for ancestor in target.parents:
            if ancestor == vault:
                break
            if ancestor.exists() and not ancestor.is_dir():
                raise SyncBlocked(f"file and directory conflict: {safe}")
        if target.is_dir():
            item = next((item for item in plan.items if item.path == safe), None)
            if item is not None and item.delete:
                raise SyncBlocked(f"cannot delete a directory through sync: {safe}")
            raise SyncBlocked(f"file and directory conflict: {safe}")
        prefix = safe + "/"
        if any(other.startswith(prefix) for other in destination_set):
            raise SyncBlocked(f"file and directory conflict: {safe}")
        for item in plan.items:
            if item.path != safe or item.data is None:
                continue
            if safe.lower().endswith(".md"):
                try:
                    item.data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SyncBlocked(f"non-UTF-8 Markdown cannot be applied: {safe}") from exc


def _transaction_from_plan(paths: SyncPaths, plan: SyncPlan, master: bytes, sync_id: str,
                           *, generation: int = 0, remote_head: str | None = None) -> tuple[dict, int]:
    """Persist encrypted object references, never plaintext plan contents."""
    objects = paths.repo / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    _enc_key, name_key, key_id = derive_keys(master)
    encrypted = 0
    serialised_items: list[dict] = []
    for item in plan.items:
        write_sha = _sha256(item.data) if item.data is not None else None
        object_ref = None
        if item.data is not None:
            object_ref = object_id(name_key, item.path)
            object_path = objects / f"{object_ref}.enc"
            reuse = False
            if object_path.exists():
                try:
                    existing = decrypt_object(master, sync_id, object_ref, object_path.read_bytes())
                    reuse = _sha256(existing) == write_sha
                except (OSError, SyncBlocked):
                    reuse = False
            if not reuse:
                atomic_write(object_path, encrypt_object(master, sync_id, object_ref, item.data))
                encrypted += 1
            item.object_id = object_ref
        serialised_items.append({
            "path": item.path,
            "expected_sha256": item.expected_sha,
            "write_sha256": write_sha,
            "delete": item.delete,
            "source": item.source,
            "object_id": object_ref,
        })
    transaction = {
        "version": 2,
        "generation": generation,
        "remote_head": remote_head,
        "items": serialised_items,
        "completed": [],
        "publish": plan.publish,
        "base_after": plan.base_after,
        "conflicts": plan.conflicts,
        "conflict_paths": plan.conflict_paths,
        "remote_secret_paths": plan.remote_secret_paths,
        "key_id": key_id.hex(),
    }
    _write_json(paths.transaction, transaction)
    return transaction, encrypted


def _plan_from_transaction(transaction: dict) -> SyncPlan:
    if transaction.get("version") != 2 or not isinstance(transaction.get("items"), list):
        raise SyncBlocked("sync transaction format is invalid; it must be discarded")
    items: list[PlanItem] = []
    for raw in transaction["items"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise SyncBlocked("sync transaction contains an invalid item")
        if not raw.get("delete") and not re.fullmatch(r"[0-9a-f]{32}", str(raw.get("object_id", ""))):
            raise SyncBlocked("sync transaction is missing an encrypted object reference")
        items.append(PlanItem(
            raw["path"],
            None,
            raw.get("expected_sha256"),
            bool(raw.get("delete")),
            str(raw.get("source", "local")),
            str(raw["object_id"]) if raw.get("object_id") else None,
        ))
    return SyncPlan(
        items=items,
        publish=transaction.get("publish", {}) if isinstance(transaction.get("publish"), dict) else {},
        base_after=transaction.get("base_after", {}) if isinstance(transaction.get("base_after"), dict) else {},
        conflicts=int(transaction.get("conflicts", 0) or 0),
        conflict_paths=[str(p) for p in transaction.get("conflict_paths", []) if isinstance(p, str)],
        remote_secret_paths=[str(p) for p in transaction.get("remote_secret_paths", []) if isinstance(p, str)],
    )


def _load_transaction(paths: SyncPaths) -> tuple[dict, SyncPlan] | None:
    if not paths.transaction.exists():
        return None
    transaction = _read_json(paths.transaction)
    if not transaction:
        paths.transaction.unlink(missing_ok=True)
        return None
    try:
        return transaction, _plan_from_transaction(transaction)
    except SyncBlocked:
        if transaction.get("version") != 2:
            paths.transaction.unlink(missing_ok=True)
            return None
        raise


def _planned_data(paths: SyncPaths, master: bytes, sync_id: str, item: PlanItem) -> bytes:
    if not item.object_id:
        raise SyncBlocked(f"sync transaction has no object for {item.path}")
    object_path = paths.repo / "objects" / f"{item.object_id}.enc"
    if not object_path.exists():
        raise SyncBlocked(f"sync transaction object is missing for {item.path}")
    return decrypt_object(master, sync_id, item.object_id, object_path.read_bytes())


def _apply_plan(config, paths: SyncPaths, plan: SyncPlan, master: bytes, sync_id: str) -> list[str]:
    transaction_info = _load_transaction(paths)
    if transaction_info is None:
        raise SyncError("application plan was not persisted")
    transaction, persisted = transaction_info
    plan = persisted
    _validate_plan_paths(config, plan, set(plan.publish) | {item.path for item in plan.items})
    completed = {str(path) for path in transaction.get("completed", []) if isinstance(path, str)}
    written: list[str] = []
    try:
        with vault_lock(config, timeout=10):
            for item in plan.items:
                target = config.vault / item.path
                if item.path in completed:
                    expected_after = None if item.delete else str(transaction_item(transaction, item.path).get("write_sha256"))
                    if _current_sha(target) != expected_after:
                        raise PlanChanged(f"vault changed after planned write: {item.path}")
                    written.append(item.path)
                    continue
                if _current_sha(target) != item.expected_sha:
                    raise PlanChanged(f"vault changed while applying plan: {item.path}")
                if item.delete:
                    if target.exists():
                        target.unlink()
                else:
                    data = _planned_data(paths, master, sync_id, item)
                    if _sha256(data) != str(transaction_item(transaction, item.path).get("write_sha256")):
                        raise SyncBlocked(f"sync transaction object hash does not match: {item.path}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.is_symlink():
                        raise PlanChanged(f"refusing to replace symlink: {item.path}")
                    atomic_write(target, data)
                written.append(item.path)
                completed.add(item.path)
                transaction["completed"] = [i.path for i in plan.items if i.path in completed]
                _write_json(paths.transaction, transaction)
    except PlanChanged:
        paths.transaction.unlink(missing_ok=True)
        raise
    return written


def transaction_item(transaction: dict, path: str) -> dict:
    for item in transaction.get("items", []):
        if isinstance(item, dict) and item.get("path") == path:
            return item
    raise SyncBlocked(f"sync transaction item is missing: {path}")


def _write_encrypted_tree(paths: SyncPaths, config, master: bytes, sync_id: str, files: dict[str, dict], generation: int,
                          previous_hash: str | None, *, reuse_commit: str | None = None) -> tuple[str, str, int, str, dict[str, dict]]:
    objects = paths.repo / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    _enc_key, name_key, _kid = derive_keys(master)
    out_files: dict[str, dict] = {}
    encrypted = 0
    for rel, entry in sorted(files.items()):
        obj = object_id(name_key, rel)
        entry = dict(entry)
        entry["object"] = obj
        data: bytes | None = None
        target = config.vault / rel
        if target.exists() and not target.is_symlink():
            candidate = target.read_bytes()
            if _sha256(candidate) == entry["sha256"]:
                data = candidate
        object_path = objects / f"{obj}.enc"
        if data is None and object_path.exists():
            try:
                candidate = decrypt_object(master, sync_id, obj, object_path.read_bytes())
                if _sha256(candidate) == entry["sha256"] and len(candidate) == int(entry["size"]):
                    data = candidate
            except SyncBlocked:
                data = None
        if data is None and reuse_commit:
            try:
                candidate = _read_object_at(paths, master, sync_id, reuse_commit, obj)
                if _sha256(candidate) == entry["sha256"] and len(candidate) == int(entry["size"]):
                    atomic_write(object_path, _git_show(paths, reuse_commit, f"objects/{obj}.enc"))
                    data = candidate
            except SyncBlocked:
                data = None
        if data is None:
            raise SyncBlocked(f"file changed or encrypted object missing before encryption: {rel}")
        if not object_path.exists():
            atomic_write(object_path, encrypt_object(master, sync_id, obj, data))
            encrypted += 1
        else:
            try:
                existing = decrypt_object(master, sync_id, obj, object_path.read_bytes())
                if _sha256(existing) != entry["sha256"] or len(existing) != int(entry["size"]):
                    atomic_write(object_path, encrypt_object(master, sync_id, obj, data))
                    encrypted += 1
            except SyncBlocked:
                atomic_write(object_path, encrypt_object(master, sync_id, obj, data))
                encrypted += 1
        out_files[rel] = entry
    document = _manifest_document(out_files, generation, previous_hash)
    plaintext = _json_bytes(document)
    # The content hash is kept outside the encrypted document body only in the
    # private state. The manifest itself is authenticated by AES-GCM.
    atomic_write(paths.repo / "manifest.enc", encrypt_manifest(master, sync_id, generation, plaintext))
    encrypted += 1
    return _sha256(plaintext), _sha256(_json_bytes(out_files)), encrypted, _kid.hex(), out_files


def _git_commit(paths: SyncPaths, *, local_head: str | None, remote_head: str | None) -> str:
    _git(paths, "add", "--all", "--", "format.json", "manifest.enc", "objects")
    tree = _git_text(paths, "write-tree")
    parents: list[str] = []
    for parent in (local_head, remote_head):
        if parent and parent not in parents:
            parents.append(parent)
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    env = _git_env()
    env.update({
        "GIT_AUTHOR_NAME": "bindery", "GIT_AUTHOR_EMAIL": "bindery@invalid",
        "GIT_COMMITTER_NAME": "bindery", "GIT_COMMITTER_EMAIL": "bindery@invalid",
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
    })
    command = ["git", *(_git_options(paths)), "-C", str(paths.repo), *args]
    result = subprocess.run(command, input=b"sync\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False, timeout=MAX_GIT_TIMEOUT)
    if result.returncode:
        raise SyncError(_short_error(result.stderr.decode("utf-8", "replace")))
    commit = result.stdout.decode("ascii", "replace").strip()
    _git(paths, "update-ref", f"refs/heads/{DEFAULT_BRANCH}", commit)
    return commit


def _push(paths: SyncPaths, commit: str, *, force: bool = False,
          expected_remote: str | None = None) -> None:
    if force and expected_remote:
        # Rekey replaces the remote root, but only if the root observed after
        # the preflight sync is still current.  A lease turns a concurrent
        # peer push into a rejected push instead of silently overwriting it.
        force_option = [f"--force-with-lease=refs/heads/{DEFAULT_BRANCH}:{expected_remote}"]
    else:
        force_option = ["--force"] if force else []
    args = ["push"] + force_option + ["bindery", f"{commit}:refs/heads/{DEFAULT_BRANCH}"]
    try:
        result = _git(paths, *args, check=False, timeout=_network_timeout())
    except SyncTimeout:
        # A timeout does not tell us whether the server accepted the ref. A
        # fetch makes that result observable without inventing a second commit.
        try:
            _fetch(paths)
            if _remote_head(paths) == commit:
                return
        except SyncError:
            pass
        raise
    if result.returncode:
        raise SyncError(_short_error(result.stderr.decode("utf-8", "replace")))


def _git_bare(paths: SyncPaths, git_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["git", *(_git_options(paths)), "--git-dir", str(git_dir), *args]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
            timeout=MAX_GIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncTimeout(f"git operation timed out: {_short_error(exc)}") from exc
    except OSError as exc:
        raise SyncError(f"git operation failed: {_short_error(exc)}") from exc
    if check and result.returncode:
        raise SyncError(_short_error(result.stderr.decode("utf-8", "replace")))
    return result


def vault_lock(config, timeout: float = 10.0):
    """The lock shared by sync and all Bindery vault writes."""
    paths = paths_for(config)
    lock = paths.root.parent.parent / "locks" / f"vault-{vault_key(config.vault)}.lock"
    return file_lock(lock, Path(config.state_dir), timeout)


def sync_lock(config, timeout: float = 10.0):
    paths = paths_for(config)
    lock = paths.root.parent.parent / "locks" / f"sync-{vault_key(config.vault)}.lock"
    return file_lock(lock, Path(config.state_dir), timeout)


def _check_rollback(state: dict, remote_doc: dict | None, remote_hash: str | None, generation: int | None,
                    *, paths: SyncPaths | None = None, master: bytes | None = None, sync_id: str = "",
                    remote_commit: str | None = None) -> None:
    accepted_generation = int(state.get("accepted_generation", 0))
    accepted_hash = str(state.get("accepted_manifest", ""))
    if remote_doc is None:
        if accepted_generation:
            raise SyncBlocked("remote manifest disappeared")
        return
    if generation is None or generation < accepted_generation:
        raise SyncBlocked("remote manifest generation rolled back")
    if generation == accepted_generation and accepted_hash and remote_hash != accepted_hash:
        raise SyncBlocked("remote manifest changed at an already accepted generation")
    if generation == accepted_generation:
        return
    if not paths or master is None or not remote_commit:
        raise SyncBlocked("cannot verify remote manifest history")

    candidates = _manifest_candidates(paths, master, sync_id, remote_commit)
    current_hash = remote_hash or ""
    current_doc = remote_doc
    current_generation = int(generation)
    seen: set[str] = set()
    while current_generation > accepted_generation:
        if current_hash in seen:
            raise SyncBlocked("remote manifest history contains a cycle")
        seen.add(current_hash)
        previous = current_doc.get("previous_hash")
        if current_generation == 1:
            if accepted_generation != 0 or previous not in (None, ""):
                raise SyncBlocked("remote manifest history is disconnected")
            return
        if not isinstance(previous, str) or not previous:
            raise SyncBlocked("remote manifest history is disconnected")
        previous_entry = candidates.get(previous)
        if previous_entry is None:
            raise SyncBlocked("remote manifest history is disconnected")
        previous_doc, previous_generation = previous_entry
        if previous_generation != current_generation - 1:
            raise SyncBlocked("remote manifest generation chain has a gap")
        current_hash = previous
        current_doc = previous_doc
        current_generation = previous_generation
    if accepted_generation:
        if current_hash != accepted_hash:
            raise SyncBlocked("remote manifest history does not reach the accepted checkpoint")
    elif current_generation != 0:
        raise SyncBlocked("remote manifest history does not reach its root")


def _base_from_state(state: dict) -> dict:
    value = state.get("base_manifest")
    return value if isinstance(value, dict) else {"generation": 0, "previous_hash": None, "files": {}}


def _local_manifest_with_objects(config, master: bytes, sync_id: str, files: dict[str, dict]) -> dict[str, dict]:
    _enc_key, name_key, _kid = derive_keys(master)
    return {
        rel: {**entry, "object": object_id(name_key, rel)}
        for rel, entry in files.items()
    }


def _record_encryptions(state: dict, key_id: str, count: int) -> None:
    if count <= 0:
        return
    counts = state.get("encryption_counts")
    if not isinstance(counts, dict):
        counts = {}
    counts = {str(key): int(value or 0) for key, value in counts.items()}
    counts[key_id] = counts.get(key_id, 0) + count
    state["encryption_counts"] = counts
    state["encryption_count"] = sum(counts.values())


def _index_after_sync(config, written: list[str]) -> str:
    """Update the index and return a state value without advancing a marker."""
    # No vault lock here. The index is derived data with its own SQLite
    # locking, and holding the vault lock while embedding made every agent's
    # memory_learn time out for minutes. Only the files this sync wrote are
    # forced; the plain reindex is hash-based, so unchanged notes keep their
    # vectors instead of being re-embedded on every five-minute run.
    store = None
    try:
        store = Store(config.db_path)
        for rel in sorted(set(written)):
            if rel.lower().endswith(".md"):
                index_path(config, store, config.vault / rel, force=True)
        reindex(config, store)
        refresh_embeddings(config, store)
        if written and store.ann_enabled:
            store.rebuild_vector_index()
        store.commit()
        if config.semantic:
            from .embed import load_backend
            if load_backend() is None:
                return "skipped"
        return "ok"
    finally:
        if store is not None:
            store.close()


def _status_from_state(config, state: dict | None = None, *, include_paths: bool = False) -> dict:
    if state is None:
        state = _load_state(paths_for(config))
    scheduler = scheduler_status(config)
    status = {
        "configured": bool(state.get("configured")),
        "remote": state.get("remote_display") or state.get("remote"),
        "remote_connection": state.get("remote"),
        "machine": state.get("machine"),
        "sync_id": state.get("sync_id"),
        "key_store": state.get("key_store"),
        "last_success": state.get("last_success"),
        "last_attempt": state.get("last_attempt"),
        "git": state.get("git", "never"),
        "index": state.get("index", "never"),
        "last_error": state.get("last_error"),
        "blocked_reason": state.get("blocked_reason"),
        "conflicts": int(state.get("conflicts", 0)),
        "held_secrets": len(state.get("held_secrets", [])) if isinstance(state.get("held_secrets"), list) else int(state.get("held_secrets", 0) or 0),
        "remote_secret_paths": sorted(str(p) for p in state.get("remote_secret_paths", []) if p),
        "encryption_counts": {str(k): int(v or 0) for k, v in (state.get("encryption_counts") or {}).items()} if isinstance(state.get("encryption_counts"), dict) else {},
        "scheduler": scheduler[0],
        "interval_seconds": scheduler[1],
        "peers": sorted(set(str(p) for p in state.get("peers", []) if p)),
        "error": state.get("last_error") or state.get("blocked_reason"),
    }
    if include_paths:
        status["held_paths"] = _held_paths(state)
        try:
            status["conflict_paths"] = _conflict_paths(config.vault)
        except OSError:
            status["conflict_paths"] = []
            status["error"] = status["error"] or "Vault 内の衝突コピーを確認できません"
    return status


def _held_paths(state: dict) -> list[str]:
    held = state.get("held_secrets")
    paths = set()
    if isinstance(held, list):
        for path in held:
            if not isinstance(path, str):
                continue
            try:
                paths.add(_safe_rel(path))
            except SyncBlocked:
                continue
    return sorted(paths)


def _conflict_paths(vault: Path) -> list[str]:
    # Conflict copies remain actionable even after a later sync reports zero
    # new conflicts. Enumerate names on disk without opening their contents.
    vault = vault.resolve()
    return sorted(
        path.relative_to(vault).as_posix()
        for path in vault.rglob("*.conflict-*")
        if re.search(r"\.conflict-[0-9a-f]{12}(?:\.[^.]+)?$", path.name)
        and not path.is_symlink()
        and path.is_file()
    )


def sync_status(config, *, include_paths: bool = False) -> dict:
    try:
        return _status_from_state(config, include_paths=include_paths)
    except Exception as exc:
        status = _status_from_state(config, {}, include_paths=include_paths)
        status["error"] = _short_error(exc)
        return status


def init_sync(config, remote_value: str, *, write: bool = False, allow_public_host: bool = False,
              key_store: str | None = None) -> SyncResult:
    try:
        requested_key_store = _key_store_choice(key_store)
        if (config.vault / ".git").exists():
            raise SyncBlocked("Vault must not contain a .git directory for encrypted sync")
        remote = validate_remote(remote_value, allow_public_host=allow_public_host)
        paths = paths_for(config)
        current = _load_state(paths)
        if current.get("configured") and current.get("remote") != remote.value:
            raise SyncBlocked("this vault is already paired with a different remote")
        if write:
            config.vault.mkdir(parents=True, exist_ok=True)
            paths.root.mkdir(parents=True, exist_ok=True)
            label = machine_label(config)
            _register_machine(config, label, write=False)
            _initialise_repo(paths, remote, write=True)
            _ensure_format(paths, write=True)
            sync_id = str(current.get("sync_id") or uuid.uuid4())
            state = {
                **current,
                "configured": True,
                "remote": remote.value,
                "remote_display": remote.display or remote.value,
                "sync_id": sync_id,
                "branch": DEFAULT_BRANCH,
                "machine": label,
                "last_attempt": current.get("last_attempt"),
                "git": current.get("git", "never"),
                "index": current.get("index", "never"),
                "blocked_reason": None,
                "last_error": None,
            }
            key = None
            try:
                key = load_key(config, state, paths, key_store=key_store)
            except SyncBlocked:
                key = os.urandom(32)
                store_key(config, state, paths, key, key_store=requested_key_store)
            with vault_lock(config):
                _register_machine(config, state["machine"], write=True)
            _save_state(paths, state)
            return SyncResult(0, _status_from_state(config, state), "sync initialized")
        # Dry run performs all non-mutating validation and reports the first
        # files that would be sent without creating the key or repository.
        files, held, unreadable = _scan_vault(config)
        if unreadable:
            raise SyncBlocked("unreadable Markdown would be excluded: " + ", ".join(unreadable))
        names = sorted(files)
        status = _status_from_state(config, current)
        status["error"] = None
        return SyncResult(0, status, "would initialize encrypted sync; files: " + (", ".join(names) if names else "(none)"))
    except SyncBlocked as exc:
        state = _load_state(paths_for(config))
        state.update({"last_attempt": _now(), "git": "blocked", "blocked_reason": _short_error(exc), "last_error": _short_error(exc)})
        if write:
            _save_state(paths_for(config), state)
        status = _status_from_state(config, state)
        status["error"] = _short_error(exc)
        return SyncResult(2, status, _short_error(exc))
    except (SyncError, OSError) as exc:
        status = sync_status(config)
        status["error"] = _short_error(exc)
        return SyncResult(1, status, _short_error(exc))


def run_sync(config, *, allow_public_host: bool = False, key_store: str | None = None,
             include_paths: bool = False) -> SyncResult:
    paths = paths_for(config)
    state = _load_state(paths)
    state["last_attempt"] = _now()
    try:
        _key_store_choice(key_store)
        if (config.vault / ".git").exists():
            raise SyncBlocked("Vault must not contain a .git directory for encrypted sync")
        if not state.get("configured"):
            raise SyncBlocked("sync is not initialized; run bindery sync init --remote ... --write")
        remote = validate_remote(str(state.get("remote", "")), allow_public_host=allow_public_host)
        paths.root.mkdir(parents=True, exist_ok=True)
        _initialise_repo(paths, remote, write=True)
        _point_repo_at(paths, remote)
        _ensure_format(paths, write=True)
        master = load_key(config, state, paths, key_store=key_store)
        label = str(state.get("machine") or machine_label(config))
        _register_machine(config, label, write=False)
        with vault_lock(config):
            _register_machine(config, label, write=True)
        with sync_lock(config):
            for _attempt in range(MAX_SYNC_ATTEMPTS):
                local_files, held, unreadable = _scan_vault(config)
                if unreadable:
                    raise SyncBlocked("cannot inspect Markdown before encryption: " + ", ".join(unreadable))
                _fetch(paths)
                remote_head = _remote_head(paths)
                remote_info = _read_remote_manifest(paths, master, str(state["sync_id"]), remote_head)
                remote_doc, remote_hash, remote_generation = remote_info if remote_info else (None, None, 0)
                _check_rollback(
                    state, remote_doc, remote_hash, remote_generation,
                    paths=paths, master=master, sync_id=str(state["sync_id"]), remote_commit=remote_head,
                )
                tracked_outside_boundary = [
                    rel for rel in _manifest_files(remote_doc or {})
                    if rel != ".bindery-sync/machines.json" and _boundary_excluded(rel, config)
                ]
                if tracked_outside_boundary:
                    raise SyncBlocked(
                        "boundary-excluded paths are still tracked; remove them deliberately before syncing: "
                        + ", ".join(sorted(tracked_outside_boundary))
                    )
                base_doc = _base_from_state(state)

                # A push may have completed after the process lost its reply.
                # Confirming the pending commit here advances the checkpoint
                # without manufacturing a second commit.
                pending = state.get("pending_commit")
                if isinstance(pending, dict) and pending.get("commit") and pending.get("commit") != remote_head:
                    if _git_head(paths) == pending.get("commit"):
                        try:
                            _push(paths, str(pending["commit"]))
                        except SyncError:
                            pass
                        try:
                            _fetch(paths)
                            remote_head = _remote_head(paths)
                            remote_info = _read_remote_manifest(paths, master, str(state["sync_id"]), remote_head)
                            remote_doc, remote_hash, remote_generation = remote_info if remote_info else (None, None, 0)
                            _check_rollback(
                                state,
                                remote_doc,
                                remote_hash,
                                remote_generation,
                                paths=paths,
                                master=master,
                                sync_id=str(state["sync_id"]),
                                remote_commit=remote_head,
                            )
                        except SyncError:
                            pass
                if isinstance(pending, dict) and pending.get("commit") == remote_head:
                    if pending.get("manifest_hash") != remote_hash:
                        raise SyncBlocked("pending commit has an unexpected manifest")
                    pending_base = pending.get("base_manifest")
                    if isinstance(pending_base, dict):
                        base_doc = pending_base
                        state.update({
                            "accepted_generation": int(pending.get("generation", remote_generation or 0)),
                            "accepted_manifest": str(pending.get("manifest_hash", remote_hash or "")),
                            "base_manifest": pending_base,
                            "base_commit": remote_head,
                        })
                    state.pop("pending_commit", None)
                    _save_state(paths, state)

                # Do not let secret-bearing local content enter the published
                # manifest. Existing base entries keep the old remote value.
                effective_files = {k: dict(v) for k, v in local_files.items()}
                for rel in held:
                    old = _manifest_entry(base_doc, rel)
                    if old:
                        effective_files[rel] = dict(old)
                    else:
                        effective_files.pop(rel, None)

                transaction_info = _load_transaction(paths)
                transaction_plan: SyncPlan | None = None
                if transaction_info is not None:
                    transaction, transaction_plan = transaction_info
                    if transaction.get("remote_head") != remote_head:
                        # The remote changed while a previous process was
                        # stopped. Replanning is safer than applying stale
                        # choices to a newer remote snapshot.
                        paths.transaction.unlink(missing_ok=True)
                        transaction_plan = None
                if transaction_plan is None:
                    local_with_objects = _local_manifest_with_objects(config, master, str(state["sync_id"]), effective_files)
                    plan = _plan(
                        config,
                        paths,
                        master,
                        str(state["sync_id"]),
                        base_doc,
                        local_with_objects,
                        remote_doc,
                        remote_head,
                        set(held),
                    )
                else:
                    plan = transaction_plan

                if plan.items:
                    if transaction_plan is None:
                        transaction, encrypted = _transaction_from_plan(
                            paths,
                            plan,
                            master,
                            str(state["sync_id"]),
                            generation=max(int(state.get("accepted_generation", 0)), int(remote_generation or 0)) + 1,
                            remote_head=remote_head,
                        )
                        _record_encryptions(state, derive_keys(master)[2].hex(), encrypted)
                        _save_state(paths, state)
                    try:
                        written = _apply_plan(config, paths, plan, master, str(state["sync_id"]))
                    except PlanChanged:
                        if _attempt + 1 < MAX_SYNC_ATTEMPTS:
                            continue
                        raise SyncBlocked("vault changed during application; three rescans were exhausted")
                else:
                    written = []
                # The applied vault is now the source of the next manifest.
                final_files, final_held, final_unreadable = _scan_vault(config)
                if final_unreadable:
                    raise SyncBlocked("cannot inspect Markdown before encryption: " + ", ".join(final_unreadable))
                # Keep held paths at their old base value for the shared view.
                local_held = set(final_held) - set(plan.remote_secret_paths)
                publish_files = _local_manifest_with_objects(
                    config,
                    master,
                    str(state["sync_id"]),
                    {k: dict(v) for k, v in final_files.items() if k not in local_held},
                )
                for rel in local_held:
                    old = _manifest_entry(base_doc, rel)
                    if old:
                        publish_files[rel] = dict(old)
                remote_files = _manifest_files(remote_doc or {})
                for rel in plan.remote_secret_paths:
                    if rel in remote_files:
                        publish_files[rel] = dict(remote_files[rel])
                local_head = _git_head(paths)
                previous_hash = str(remote_hash or state.get("accepted_manifest") or "") or None
                transport_same = {
                    path: entry.get("sha256") for path, entry in publish_files.items()
                } == {
                    path: entry.get("sha256") for path, entry in _manifest_files(remote_doc or {}).items()
                }
                if remote_head is not None and not plan.conflicts and transport_same and (local_head is None or local_head == remote_head):
                    if local_head is None:
                        _git(paths, "update-ref", f"refs/heads/{DEFAULT_BRANCH}", remote_head)
                        local_head = remote_head
                    commit = remote_head
                    generation = int(remote_generation or state.get("accepted_generation", 0))
                    manifest_hash = str(remote_hash or state.get("accepted_manifest", ""))
                    committed_files = remote_files or publish_files
                    base_previous_hash = remote_doc.get("previous_hash") if remote_doc else None
                else:
                    generation = max(int(state.get("accepted_generation", 0)), int(remote_generation or 0)) + 1
                    # A fresh scan can include remote conflict copies and any
                    # non-conflicting remote changes that were applied.
                    manifest_hash, _tree_hash, encrypted, key_id, committed_files = _write_encrypted_tree(
                        paths,
                        config,
                        master,
                        str(state["sync_id"]),
                        publish_files,
                        generation,
                        previous_hash,
                        reuse_commit=remote_head or local_head,
                    )
                    _record_encryptions(state, key_id, encrypted)
                    commit = _git_commit(paths, local_head=local_head, remote_head=remote_head)
                    base_previous_hash = previous_hash
                    state["pending_commit"] = {
                        "commit": commit,
                        "generation": generation,
                        "manifest_hash": manifest_hash,
                        "base_manifest": {
                            "generation": generation,
                            "previous_hash": previous_hash,
                            "files": committed_files,
                        },
                        "written": sorted(set(written)),
                    }
                    _save_state(paths, state)
                try:
                    _push(paths, commit)
                except SyncError:
                    # Non-fast-forward: a fresh fetch and a new plan are safer
                    # than attempting a textual merge of ciphertext trees.
                    if _attempt + 1 < MAX_SYNC_ATTEMPTS:
                        continue
                    raise
                state.pop("pending_commit", None)
                state.update({
                    "git": "ok",
                    "accepted_generation": generation,
                    "accepted_manifest": manifest_hash,
                    "base_manifest": {
                        "generation": generation,
                        "previous_hash": base_previous_hash,
                        "files": committed_files,
                    },
                    "base_commit": commit,
                    "held_secrets": sorted(local_held),
                })
                _save_state(paths, state)

                # Git and indexing are separate states. In particular, do not
                # advance applied_* until every index operation succeeds.
                index_status = "ok"
                try:
                    index_status = _index_after_sync(config, written)
                except Exception as exc:
                    index_status = "error"
                    state["last_error"] = _short_error(f"index update: {exc}")
                state.update({
                    "index": index_status,
                    "blocked_reason": None,
                    "conflicts": len([p for p in plan.conflict_paths if (config.vault / p).exists()]),
                    "remote_secret_paths": sorted(set(plan.remote_secret_paths)),
                    "last_error": state.get("last_error") if index_status == "error" else None,
                })
                if index_status in {"ok", "skipped"}:
                    state.update({
                        "last_success": _now(),
                        "applied_generation": generation,
                        "applied_manifest": manifest_hash,
                    })
                key_id = derive_keys(master)[2].hex()
                if int(state.get("encryption_counts", {}).get(key_id, 0)) >= 2**32:
                    state["last_error"] = "encryption count reached 2^32; rekey is required"
                _save_state(paths, state)
                paths.transaction.unlink(missing_ok=True)
                status = _status_from_state(config, state, include_paths=include_paths)
                return SyncResult(1 if index_status == "error" else 0, status, "sync complete")
            raise SyncBlocked("vault changed during application; three rescans were exhausted")
        raise SyncError("sync attempts exhausted")
    except LockTimeout as exc:
        state.update({"git": "blocked", "blocked_reason": "vault or sync lock timeout", "last_error": _short_error(exc)})
        _save_state(paths, state)
        status = _status_from_state(config, state, include_paths=include_paths)
        status["error"] = _short_error(exc)
        return SyncResult(2, status, _short_error(exc))
    except SyncBlocked as exc:
        state.update({"git": "blocked", "blocked_reason": _short_error(exc), "last_error": _short_error(exc)})
        _save_state(paths, state)
        status = _status_from_state(config, state, include_paths=include_paths)
        status["error"] = _short_error(exc)
        return SyncResult(2, status, _short_error(exc))
    except (SyncError, OSError) as exc:
        state.update({"git": "error", "last_error": _short_error(exc)})
        _save_state(paths, state)
        status = _status_from_state(config, state, include_paths=include_paths)
        status["error"] = _short_error(exc)
        return SyncResult(1, status, _short_error(exc))


def key_export(config) -> str:
    paths = paths_for(config)
    state = _load_state(paths)
    key = load_key(config, state, paths)
    return "WARNING: store this recovery string in a password manager; it is the only copy of the sync key.\n" + base64.b64encode(key).decode("ascii")


def accept_key(config, sync_id: str, payload: bytes, *, replace: bool = False,
               key_store: str | None = None) -> SyncResult:
    paths = paths_for(config)
    try:
        data = json.loads(payload.decode("utf-8"))
        key = base64.b64decode(str(data["key"]), validate=True)
        if len(key) != 32 or str(data["sync_id"]) != sync_id:
            raise SyncBlocked("pairing payload is invalid")
        state = _load_state(paths)
        existing = state.get("sync_id")
        if existing and existing == sync_id:
            try:
                old = load_key(config, state, paths, key_store=key_store)
                if not hmac.compare_digest(old, key) and not replace:
                    raise SyncBlocked("a different key is already registered; pass --replace")
            except SyncBlocked:
                if not replace and paths.key.exists():
                    raise
        remote_value = str(data.get("remote", "")).strip()
        remote = validate_remote(remote_value) if remote_value else None
        checkpoint = data.get("checkpoint") if isinstance(data.get("checkpoint"), dict) else data
        state.update({
            "configured": True,
            "sync_id": sync_id,
            "remote": remote.value if remote else state.get("remote"),
            "remote_display": remote.display if remote else state.get("remote_display"),
            "machine": machine_label(config),
            "accepted_generation": int(checkpoint.get("generation", 0)),
            "accepted_manifest": str(checkpoint.get("manifest_hash", "")),
            "blocked_reason": None,
            "last_error": None,
        })
        if not state.get("remote"):
            raise SyncBlocked("pairing payload did not include a remote URL")
        paths.root.mkdir(parents=True, exist_ok=True)
        store_key(config, state, paths, key, replace=replace, key_store=key_store)
        _save_state(paths, state)
        return SyncResult(0, _status_from_state(config, state), "key accepted")
    except SyncBlocked as exc:
        status = _status_from_state(config)
        status["error"] = _short_error(exc)
        return SyncResult(2, status, _short_error(exc))
    except Exception as exc:
        status = _status_from_state(config)
        status["error"] = _short_error(exc)
        return SyncResult(1, status, _short_error(exc))


def _validate_host_argument(host: str) -> str:
    value = str(host or "")
    if not value or value.startswith("-") or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise SyncError("SSH host must not start with '-' or contain control characters")
    return value


def _remote_command(executable: str | None, args: list[str]) -> str:
    if executable:
        executable_part = shlex.quote(executable)
        return "exec " + executable_part + " " + " ".join(shlex.quote(arg) for arg in args)
    # Resolve on the peer, never by reusing the local machine's executable.
    return "bindery_path=$(command -v bindery) && exec \"$bindery_path\" " + " ".join(shlex.quote(arg) for arg in args)


def pair(config, host: str, *, replace: bool = False, remote_vault: str | None = None,
         remote_bindery: str | None = None, remote_key_store: str | None = None,
         key_store: str | None = None, peer_remote: str | None = None) -> SyncResult:
    paths = paths_for(config)
    state = _load_state(paths)
    key: bytes | None = None
    try:
        host = _validate_host_argument(host)
        # The remote URL is written from this machine's point of view. A peer
        # that hosts the bare repository itself, or reaches it under another
        # SSH alias, needs its own spelling; validate it here so a bad value
        # fails before the key leaves this machine.
        peer_remote_value = validate_remote(peer_remote).value if peer_remote else state.get("remote")
        key = load_key(config, state, paths, key_store=key_store)
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "--", host]
        remote_key_store = _key_store_choice(remote_key_store, use_environment=False)
        remote_args = [
            "sync", "accept-key", "--sync-id", str(state["sync_id"]), "--vault", str(remote_vault or config.vault),
            "--key-store", remote_key_store,
        ]
        if replace:
            remote_args.append("--replace")
        payload = _json_bytes({
            "sync_id": state["sync_id"],
            "key": base64.b64encode(key).decode("ascii"),
            "remote": peer_remote_value,
            "checkpoint": {
                "generation": state.get("accepted_generation", 0),
                "manifest_hash": state.get("accepted_manifest", ""),
            },
        }) + b"\n"
        remote_command = _remote_command(remote_bindery, remote_args)
        result = subprocess.run(command + [remote_command], input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else result.stderr
            raise SyncError(_short_error(detail))
        state["peers"] = sorted(set(state.get("peers", [])) | {host})
        state.update({"blocked_reason": None, "last_error": None})
        _save_state(paths, state)
        return SyncResult(0, _status_from_state(config, state), "paired")
    except SyncBlocked as exc:
        status = _status_from_state(config)
        message = _error_without_secrets(exc, (key,) if key else ())
        status["error"] = message
        return SyncResult(2, status, message)
    except (OSError, subprocess.SubprocessError, SyncError) as exc:
        status = _status_from_state(config)
        message = _error_without_secrets(exc, (key,) if key else ())
        status["error"] = message
        return SyncResult(1, status, message)


def _scheduler_paths(config) -> tuple[str, int | None, Path, Path | None]:
    key = vault_key(config.vault)
    raw_interval = _load_state(paths_for(config)).get("interval_seconds")
    interval = int(raw_interval or 300)
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"com.bindery.sync.{key}.plist"
        return "macos", interval, path, None
    service = Path.home() / ".config" / "systemd" / "user" / f"bindery-sync-{key}.service"
    timer = service.with_suffix(".timer")
    return "systemd", interval, service, timer


def scheduler_status(config) -> tuple[str, int | None]:
    kind, interval, first, second = _scheduler_paths(config)
    if kind == "macos":
        return ("installed", interval) if first.exists() else ("absent", None)
    return ("installed", interval) if first.exists() and second and second.exists() else ("absent", None)


def _scheduler_command(config) -> list[str]:
    # Do not resolve sys.executable: in a uv/pipx tool environment the venv's
    # python is a symlink to a shared interpreter, and following it leaves the
    # venv, so the job starts a Python that cannot import bindery. Prefer the
    # console script installed beside it. Options belong after `sync`; the
    # top-level parser does not accept --vault.
    interpreter = Path(sys.executable).absolute()
    beside = interpreter.parent / "bindery"
    if beside.is_file():
        program = [str(beside)]
    else:
        found = shutil.which("bindery")
        program = [str(Path(found).absolute())] if found else [str(interpreter), "-m", "bindery"]
    return [*program, "sync", "--vault", str(config.vault), "--state-dir", str(config.state_dir)]


def _systemd_quote(value: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise SyncError("scheduler paths must not contain newlines or control characters")
    value = value.replace("$", "$$").replace("%", "%%")
    if re.fullmatch(r"[A-Za-z0-9_./:+-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(" ", "\\x20") + '"'


def _run_scheduler_command(command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, timeout=15)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else result.stderr
        raise SyncError(_short_error(detail) or f"scheduler command failed: {' '.join(command)}")


def install_scheduler(config, interval: int = 300, *, write: bool = False, emit: bool = True) -> SyncResult:
    paths = paths_for(config)
    state = _load_state(paths)
    try:
        if interval <= 0:
            raise SyncError("interval must be positive")
        kind, _old, first, second = _scheduler_paths(config)
        command = " ".join(_systemd_quote(v) for v in _scheduler_command(config))
        if kind == "macos":
            import plistlib
            label = first.stem
            plist = {"Label": label, "ProgramArguments": _scheduler_command(config), "StartInterval": interval, "RunAtLoad": True, "StandardOutPath": str(paths.root / "scheduler.log"), "StandardErrorPath": str(paths.root / "scheduler.log")}
            rendered = plistlib.dumps(plist).decode("utf-8")
        else:
            rendered = f"[Unit]\nDescription=Bindery encrypted sync\n\n[Service]\nType=oneshot\nExecStart={command}\n"
            timer = f"[Unit]\nDescription=Bindery encrypted sync timer\n\n[Timer]\nOnBootSec={interval}s\nOnUnitActiveSec={interval}s\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
        if not write:
            if emit:
                print(rendered, end="")
            if kind == "systemd" and emit:
                print(timer, end="")
            return SyncResult(0, _status_from_state(config, state), "would install scheduler")
        paths.root.mkdir(parents=True, exist_ok=True)
        if kind == "macos":
            atomic_write(first, rendered)
            # bootstrap refuses a label that is already loaded (error 5) and
            # launchd keeps running the old definition, so unload it first.
            # A missing job is the normal first-install case, not an error.
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                           check=False, capture_output=True, timeout=15)
            _run_scheduler_command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(first)])
        else:
            assert second is not None
            atomic_write(first, rendered)
            atomic_write(second, timer)
            _run_scheduler_command(["systemctl", "--user", "daemon-reload"])
            _run_scheduler_command(["systemctl", "--user", "enable", "--now", second.name])
        state.update({"interval_seconds": interval, "scheduler": "installed"})
        _save_state(paths, state)
        return SyncResult(0, _status_from_state(config, state), "scheduler installed")
    except (OSError, SyncError, subprocess.SubprocessError) as exc:
        return SyncResult(1, _status_from_state(config), _short_error(exc))


def uninstall_scheduler(config, *, write: bool = False, emit: bool = True) -> SyncResult:
    paths = paths_for(config)
    state = _load_state(paths)
    try:
        kind, _interval, first, second = _scheduler_paths(config)
        if not write:
            if emit:
                print(f"would remove {first}")
            if second and emit:
                print(f"would remove {second}")
            return SyncResult(0, _status_from_state(config, state), "would uninstall scheduler")
        if kind == "macos":
            _run_scheduler_command(["launchctl", "bootout", f"gui/{os.getuid()}/{first.stem}"])
        else:
            assert second is not None
            _run_scheduler_command(["systemctl", "--user", "disable", "--now", second.name])
        first.unlink(missing_ok=True)
        if second:
            second.unlink(missing_ok=True)
        state.update({"scheduler": "absent", "interval_seconds": None})
        _save_state(paths, state)
        return SyncResult(0, _status_from_state(config, state), "scheduler uninstalled")
    except (OSError, subprocess.SubprocessError) as exc:
        return SyncResult(1, _status_from_state(config), _short_error(exc))


def rekey(config, *, write: bool = False, purge_remote: bool = False,
          key_store: str | None = None) -> SyncResult:
    paths = paths_for(config)
    state = _load_state(paths)
    if not write:
        return SyncResult(2, _status_from_state(config), "rekey is destructive; pass --write")
    if not state.get("configured") or not state.get("sync_id"):
        return SyncResult(2, _status_from_state(config), "sync is not initialized")
    try:
        # Rekeying is a root replacement, so it must never be used as a way to
        # skip an ordinary fetch/merge/apply/push cycle first.  This also makes
        # the Vault contents below the result of the latest shared state.
        preflight = run_sync(config, key_store=key_store)
        if preflight.exit_code != 0:
            return SyncResult(
                preflight.exit_code,
                _status_from_state(config),
                "rekey aborted because the required pre-sync did not succeed: " + preflight.message,
            )
        state = _load_state(paths)
        remote = validate_remote(str(state.get("remote", "")))
        if purge_remote and remote.kind not in {"file", "path"}:
            raise SyncError("--purge-remote is only available for a local remote")
        old_key = load_key(config, state, paths, key_store=key_store)
        new_key = os.urandom(32)
        new_key_id = derive_keys(new_key)[2].hex()
        pending_key = paths.root / "rekey.pending.key"
        paths.root.mkdir(parents=True, exist_ok=True)
        _key_file_write(pending_key, new_key)
        temp_root = paths.root / f".rekey-{uuid.uuid4().hex}"
        temp_paths = SyncPaths(
            root=temp_root,
            repo=temp_root / "repo",
            state=temp_root / "state.json",
            key=temp_root / "master.key",
            format=temp_root / "repo" / "format.json",
            transaction=temp_root / "transaction.json",
        )
        with vault_lock(config, timeout=10):
            with sync_lock(config, timeout=10):
                # Capture the lease immediately before building the new root.
                # The force-with-lease below closes the remaining race while
                # encryption is in progress.
                _fetch(paths)
                expected_remote = _remote_head(paths)
                local_head = _git_head(paths)
                if not expected_remote or local_head != expected_remote:
                    raise SyncBlocked("rekey requires the local HEAD and remote HEAD to be the same after sync")
                _initialise_repo(temp_paths, remote, write=True)
                _ensure_format(temp_paths, write=True)
                files, held, unreadable = _scan_vault(config)
                if unreadable:
                    raise SyncBlocked("cannot rekey unreadable Markdown: " + ", ".join(unreadable))
                for rel in held:
                    files.pop(rel, None)
                manifest_files = _local_manifest_with_objects(config, new_key, str(state["sync_id"]), files)
                manifest_hash, _tree_hash, encrypted, _key_id, committed_files = _write_encrypted_tree(
                    temp_paths,
                    config,
                    new_key,
                    str(state["sync_id"]),
                    manifest_files,
                    1,
                    None,
                )
                commit = _git_commit(temp_paths, local_head=None, remote_head=None)
                _push(temp_paths, commit, force=True, expected_remote=expected_remote)

                # The remote root is accepted before replacing local key
                # material. The old key is archived, never deleted.
                old_key_id = derive_keys(old_key)[2].hex()
                _key_file_write(paths.root / f"master.key.previous-{old_key_id}", old_key)
                store_key(config, state, paths, new_key, replace=True, key_store=key_store)
                old_repo = paths.root / f"repo.previous-{old_key_id}"
                if paths.repo.exists():
                    paths.repo.rename(old_repo)
                temp_paths.repo.rename(paths.repo)
                pending_key.unlink(missing_ok=True)
                paths.transaction.unlink(missing_ok=True)
                state.update({
                    "accepted_generation": 1,
                    "accepted_manifest": manifest_hash,
                    "applied_generation": 1,
                    "applied_manifest": manifest_hash,
                    "base_manifest": {"generation": 1, "previous_hash": None, "files": committed_files},
                    "base_commit": commit,
                    "held_secrets": sorted(held),
                    "git": "ok",
                    "index": "ok",
                    "last_success": _now(),
                    "last_attempt": _now(),
                    "blocked_reason": None,
                    "last_error": None,
                    "encryption_counts": {new_key_id: encrypted},
                    "encryption_count": encrypted,
                    "pending_commit": None,
                })
                _save_state(paths, state)
                if purge_remote and remote.path is not None:
                    _git_bare(temp_paths, remote.path, "reflog", "expire", "--expire=now", "--all")
                    _git_bare(temp_paths, remote.path, "gc", "--prune=now")
        return SyncResult(0, _status_from_state(config, state), "rekeyed; pair every other machine again")
    except SyncBlocked as exc:
        state.update({"git": "blocked", "blocked_reason": _short_error(exc), "last_error": _short_error(exc)})
        _save_state(paths, state)
        status = _status_from_state(config, state)
        status["error"] = _short_error(exc)
        return SyncResult(2, status, _short_error(exc))
    except (OSError, SyncError) as exc:
        state.update({"git": "error", "last_error": _short_error(exc)})
        _save_state(paths, state)
        status = _status_from_state(config, state)
        status["error"] = _short_error(exc)
        return SyncResult(1, status, _short_error(exc))

