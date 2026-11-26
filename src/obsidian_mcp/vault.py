"""Vault path helpers — every user-provided path is resolved through here.

Several vaults can be served at once:

- the **main vault**: `OBSIDIAN_VAULT` (local path) and `OBSIDIAN_VAULT_REMOTE` (its
  Git repo), named `OBSIDIAN_VAULT_NAME` (default "principal");
- **extra vaults**: `OBSIDIAN_EXTRA_VAULTS="cours=https://github.com/me/cours.git;
  perso=https://github.com/me/perso.git"`, each cloned into
  `OBSIDIAN_VAULTS_DIR/<name>` (default: a `vaults` folder next to the main vault).
  A value that is not a URL is taken as a local path, with no Git sync.

The vault a tool works on is held in a context variable set by :func:`use_vault`, so
the helpers below (and every module built on them) need no extra argument.

When the server is embedded in another app (a hosted service with many users), that
app hands each request its own set of vaults with :func:`use_vaults`. Inside it the
environment is ignored: a request only ever sees the vaults it was given.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path


class VaultError(Exception):
    pass


DEFAULT_MAIN_NAME = "principal"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_current: ContextVar[str | None] = ContextVar("obsidian_vault", default=None)


@dataclass(frozen=True)
class Vault:
    name: str
    path: Path
    remote: str | None
    main: bool = False
    # Embedded use: returns the password git should use for `remote` (for example a
    # short-lived GitHub App token), asked before each network operation. None means
    # the environment's OBSIDIAN_GIT_TOKEN.
    credentials: Callable[[], str | None] | None = field(default=None, compare=False, repr=False)
    # Embedded use: the author of the commits made in this vault (default: environment).
    git_name: str | None = None
    git_email: str | None = None
    # Embedded use: where the dashboard says the vault lives ("Hosted by Example").
    # None: worked out from the remote.
    label: str | None = field(default=None, compare=False)

    @property
    def key(self) -> str:
        """Identifies the vault in caches, locks and sync state. Names can repeat across
        the workspaces of an embedding app, paths can't."""
        return str(self.path)


@dataclass(frozen=True)
class VaultScope:
    vaults: dict[str, Vault]
    empty_message: str | None = None


_scope: ContextVar[VaultScope | None] = ContextVar("obsidian_vault_scope", default=None)


@contextmanager
def use_vaults(vaults: Mapping[str, Vault], *, empty_message: str | None = None) -> Iterator[None]:
    """Serve exactly these vaults to the enclosed code, instead of the environment's.

    `empty_message` is the error given when there are none (for example "connect a
    vault first"). A vault selected outside the scope does not leak into it.
    """
    for name in vaults:
        if not _NAME_RE.match(name):
            raise VaultError(f"invalid vault name {name!r} (lowercase letters, digits, - and _)")
    scope_token = _scope.set(VaultScope(dict(vaults), empty_message))
    current_token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(current_token)
        _scope.reset(scope_token)


def scoped() -> bool:
    """True inside :func:`use_vaults`."""
    return _scope.get() is not None


def _is_remote(value: str) -> bool:
    return value.startswith(("https://", "http://", "ssh://", "git@", "file://")) or value.endswith(
        ".git"
    )


def main_vault_name() -> str:
    name = (os.environ.get("OBSIDIAN_VAULT_NAME") or "").strip().lower()
    return name or DEFAULT_MAIN_NAME


def configured_vaults() -> dict[str, Vault]:
    """Every configured vault, main one first. Read from the environment on each call,
    or the vaults given to :func:`use_vaults`."""
    scope = _scope.get()
    if scope is not None:
        return dict(scope.vaults)
    vaults: dict[str, Vault] = {}
    raw_main = (os.environ.get("OBSIDIAN_VAULT") or "").strip()
    main_name = main_vault_name()
    if raw_main:
        remote = (os.environ.get("OBSIDIAN_VAULT_REMOTE") or "").strip() or None
        vaults[main_name] = Vault(main_name, Path(raw_main).expanduser(), remote, main=True)

    extra = (os.environ.get("OBSIDIAN_EXTRA_VAULTS") or "").strip()
    if not extra:
        return vaults
    base_raw = (os.environ.get("OBSIDIAN_VAULTS_DIR") or "").strip()
    if base_raw:
        base = Path(base_raw).expanduser()
    elif raw_main:
        base = Path(raw_main).expanduser().parent / "vaults"
    else:
        raise VaultError("OBSIDIAN_EXTRA_VAULTS needs OBSIDIAN_VAULTS_DIR or OBSIDIAN_VAULT")
    for item in re.split(r"[;\n]", extra):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        name, value = name.strip().lower(), value.strip()
        if not sep or not value:
            raise VaultError(f"OBSIDIAN_EXTRA_VAULTS: expected name=url, got {item!r}")
        if not _NAME_RE.match(name):
            raise VaultError(
                f"OBSIDIAN_EXTRA_VAULTS: invalid vault name {name!r} "
                "(lowercase letters, digits, - and _)"
            )
        if name in vaults:
            raise VaultError(f"OBSIDIAN_EXTRA_VAULTS: vault {name!r} is defined twice")
        if _is_remote(value):
            vaults[name] = Vault(name, base / name, value)
        else:
            vaults[name] = Vault(name, Path(value).expanduser(), None)
    return vaults


def get_vault(name: str | None = None) -> Vault:
    """The vault called `name`, or the current one (main vault if none is selected)."""
    vaults = configured_vaults()
    if not vaults:
        scope = _scope.get()
        if scope is not None:
            raise VaultError(scope.empty_message or "No vault is connected yet.")
        raise VaultError(
            "OBSIDIAN_VAULT is not set. Point it at the absolute path of your Obsidian vault."
        )
    wanted = name if name is not None else _current.get()
    if wanted is None:
        return next(iter(vaults.values()))
    wanted = wanted.strip().lower()
    if wanted not in vaults:
        raise VaultError(
            f"unknown vault {wanted!r}; available vaults: {', '.join(vaults)} "
            "(call list_vaults)"
        )
    return vaults[wanted]


@contextmanager
def use_vault(name: str | None, *, explicit: bool = False) -> Iterator[Vault]:
    """Run the enclosed code against vault `name` (main vault when None).

    With `explicit=True` (write tools), `name` may only be omitted when a single vault
    is configured, so a change never lands in a vault the user did not choose.
    """
    if explicit and name is None and len(configured_vaults()) > 1:
        raise VaultError(
            "several vaults are configured: pass `vault` explicitly for any change "
            f"({', '.join(configured_vaults())}). Ask the user which one if unsure."
        )
    vault = get_vault(name)
    token = _current.set(vault.name)
    try:
        yield vault
    finally:
        _current.reset(token)


def vault_root() -> Path:
    vault = get_vault()
    root = vault.path.resolve()
    if not root.is_dir():
        if vault.main:
            raise VaultError(f"OBSIDIAN_VAULT does not exist or is not a directory: {root}")
        raise VaultError(f"vault {vault.name!r} does not exist or is not a directory: {root}")
    return root


def resolve_inside_vault(relative: str) -> Path:
    """Resolve a vault-relative path and refuse anything that escapes the vault."""
    if not relative or relative.strip() == "":
        raise VaultError("Path is empty.")
    root = vault_root()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise VaultError(
            f"Path escapes the vault: {relative!r} resolves to {candidate}"
        ) from exc
    return candidate


def ensure_md_extension(relative: str) -> str:
    return relative if relative.lower().endswith(".md") else relative + ".md"


def relative_to_vault(path: Path) -> str:
    return str(path.resolve().relative_to(vault_root()))
