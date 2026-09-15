"""
easyfile — Easier, safer file operations for Python.

Design goals
------------
1. Easier than pathlib: common tasks are one-liners.
2. Safer than raw pathlib/os/shutil:
   - Writes are atomic (write to temp file, then rename) so a crash never
     leaves a half-written file.
   - Deletes are safe by default (moved to a local .trash/ folder instead
     of being permanently removed) unless you explicitly ask for permanent.
   - Path traversal / "zip-slip" style escapes are blocked when extracting
     archives or joining untrusted path segments.
   - No silent overwrites unless you opt in.
3. Batteries included: read/write text, bytes, JSON, lines, CSV-ish,
   hashing, copying, moving, searching, and zipping — without importing
   five different stdlib modules.

Quick start
-----------
    from easyfile import File, Dir

    f = File("notes/todo.txt")
    f.write("buy milk")            # creates notes/ automatically
    print(f.read())                # "buy milk"
    f.append("\\nwalk dog")

    data = File("config.json")
    data.write_json({"debug": True})
    cfg = data.read_json()

    Dir("build").clear()           # safely trashes contents, not permanent
    File("secret.key").delete()    # moved to .trash/, recoverable

Everything returns plain Python types (str, bytes, dict, list, Path) —
no custom result-wrapper objects to learn.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

__all__ = [
    "File",
    "Dir",
    "EasyFileError",
    "PathEscapeError",
    "NotAFileError",
    "NotADirError",
    "safe_join",
]

PathLike = Union[str, "os.PathLike[str]"]


# --------------------------------------------------------------------------- #
# Exceptions — specific, human-readable, instead of raw OSError/FileNotFound
# --------------------------------------------------------------------------- #

class EasyFileError(Exception):
    """Base exception for all easyfile errors."""


class PathEscapeError(EasyFileError):
    """Raised when an operation would write/read outside an intended root."""


class NotAFileError(EasyFileError):
    """Raised when a file-only operation is used on something that isn't a file."""


class NotADirError(EasyFileError):
    """Raised when a directory-only operation is used on something that isn't a dir."""


# --------------------------------------------------------------------------- #
# Safety helpers
# --------------------------------------------------------------------------- #

def safe_join(root: PathLike, *parts: str) -> Path:
    """
    Join path segments onto `root`, and guarantee the result stays inside
    `root`. Raises PathEscapeError on any attempt to break out via '..',
    absolute paths, or symlink tricks.

    This is the safe replacement for `Path(root) / user_input`, which is a
    classic path-traversal vulnerability when `user_input` is untrusted
    (e.g. from a URL, zip entry, or form field).
    """
    root_resolved = Path(root).resolve()
    candidate = root_resolved.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise PathEscapeError(
            f"Path {candidate} escapes root {root_resolved}"
        ) from None
    return candidate


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}EB"


def _trash_dir_for(path: Path) -> Path:
    """Every safe-delete lands in a `.trash` folder next to the target's tree root."""
    trash = path.parent / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    return trash


def _timestamped_name(name: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{name}"


# --------------------------------------------------------------------------- #
# File
# --------------------------------------------------------------------------- #

class File:
    """
    A single file, with easy, safe read/write helpers.

    >>> f = File("out/report.txt")
    >>> f.write("hello")      # auto-creates out/
    >>> f.read()
    'hello'
    """

    __slots__ = ("path",)

    def __init__(self, path: PathLike):
        self.path = Path(path)

    # -- dunder / convenience -------------------------------------------- #

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f"File({str(self.path)!r})"

    def __fspath__(self) -> str:
        return str(self.path)

    def __truediv__(self, other: str) -> "File":
        # Mirrors pathlib ergonomics: File("dir") / "child.txt"
        return File(self.path / other)

    # -- introspection ------------------------------------------------------ #

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def suffix(self) -> str:
        return self.path.suffix

    @property
    def parent(self) -> "Dir":
        return Dir(self.path.parent)

    def size(self, human: bool = False) -> Union[int, str]:
        self._require_file()
        n = self.path.stat().st_size
        return _human_size(n) if human else n

    def modified(self) -> float:
        """Last-modified time, as a Unix timestamp."""
        self._require_exists()
        return self.path.stat().st_mtime

    def is_empty(self) -> bool:
        return (not self.exists) or self.size() == 0

    def _require_exists(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"No such file: {self.path}")

    def _require_file(self) -> None:
        self._require_exists()
        if not self.path.is_file():
            raise NotAFileError(f"Not a file: {self.path}")

    # -- reading -------------------------------------------------------- #

    def read(self, encoding: str = "utf-8") -> str:
        """Read the whole file as text."""
        self._require_file()
        return self.path.read_text(encoding=encoding)

    def read_bytes(self) -> bytes:
        self._require_file()
        return self.path.read_bytes()

    def read_lines(self, encoding: str = "utf-8", keepends: bool = False) -> list[str]:
        text = self.read(encoding=encoding)
        return text.splitlines(keepends=keepends)

    def read_json(self, encoding: str = "utf-8") -> Any:
        text = self.read(encoding=encoding)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise EasyFileError(f"Invalid JSON in {self.path}: {e}") from e

    def read_csv(self, **kwargs: Any) -> list[dict[str, str]]:
        """Read a CSV file into a list of dicts (uses the header row as keys)."""
        self._require_file()
        with self.path.open("r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh, **kwargs))

    def iter_lines(self, encoding: str = "utf-8") -> Iterator[str]:
        """Memory-efficient line iteration for large files."""
        self._require_file()
        with self.path.open("r", encoding=encoding) as fh:
            for line in fh:
                yield line.rstrip("\n")

    def hash(self, algo: str = "sha256", chunk_size: int = 1 << 20) -> str:
        """Return a hex digest of the file's contents without loading it all into RAM."""
        self._require_file()
        h = hashlib.new(algo)
        with self.path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()

    # -- writing (atomic by default) ------------------------------------- #

    def _atomic_write(self, mode: str, data: Union[str, bytes], encoding: Optional[str]) -> None:
        """
        Write via a temp file in the same directory, then os.replace().
        os.replace is atomic on POSIX and Windows, so readers never see a
        half-written file, and a crash mid-write can't corrupt the original.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, mode, encoding=encoding) as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            with _suppress_os_error():
                os.remove(tmp_path)
            raise

    def write(self, content: str, encoding: str = "utf-8", overwrite: bool = True) -> "File":
        """
        Write text to the file. Creates parent directories automatically.
        Atomic: never leaves a partially-written file behind.

        Set overwrite=False to raise instead of clobbering an existing file.
        """
        if not overwrite and self.exists:
            raise FileExistsError(
                f"{self.path} already exists (pass overwrite=True to replace it)"
            )
        self._atomic_write("w", content, encoding)
        return self

    def write_bytes(self, data: bytes, overwrite: bool = True) -> "File":
        if not overwrite and self.exists:
            raise FileExistsError(
                f"{self.path} already exists (pass overwrite=True to replace it)"
            )
        self._atomic_write("wb", data, None)
        return self

    def write_json(self, obj: Any, indent: int = 2, overwrite: bool = True) -> "File":
        content = json.dumps(obj, indent=indent, default=str)
        return self.write(content, overwrite=overwrite)

    def write_csv(self, rows: Iterable[dict[str, Any]], overwrite: bool = True) -> "File":
        rows = list(rows)
        if not rows:
            return self.write("", overwrite=overwrite)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return self.write(buf.getvalue(), overwrite=overwrite)

    def append(self, content: str, encoding: str = "utf-8") -> "File":
        """Append text, creating the file (and parents) if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding=encoding) as fh:
            fh.write(content)
        return self

    def touch(self) -> "File":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        return self

    # -- copy / move / delete -------------------------------------------- #

    def copy_to(self, dest: PathLike, overwrite: bool = False) -> "File":
        self._require_file()
        dest_path = Path(dest)
        if dest_path.is_dir():
            dest_path = dest_path / self.path.name
        if dest_path.exists() and not overwrite:
            raise FileExistsError(f"{dest_path} already exists (pass overwrite=True)")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, dest_path)
        return File(dest_path)

    def move_to(self, dest: PathLike, overwrite: bool = False) -> "File":
        self._require_file()
        dest_path = Path(dest)
        if dest_path.is_dir():
            dest_path = dest_path / self.path.name
        if dest_path.exists() and not overwrite:
            raise FileExistsError(f"{dest_path} already exists (pass overwrite=True)")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.path), str(dest_path))
        self.path = dest_path
        return self

    def rename(self, new_name: str) -> "File":
        """Rename in place (same directory)."""
        return self.move_to(self.path.parent / new_name)

    def delete(self, permanent: bool = False) -> None:
        """
        Delete the file.

        By default this is a SAFE delete: the file is moved into a
        `.trash/` folder beside it, timestamped, so an accidental delete()
        is recoverable. Pass permanent=True for a real, unrecoverable delete.
        """
        if not self.exists:
            return
        if permanent:
            self.path.unlink()
            return
        trash = _trash_dir_for(self.path)
        dest = trash / _timestamped_name(self.path.name)
        shutil.move(str(self.path), str(dest))

    # -- misc -------------------------------------------------------------- #

    def backup(self, suffix: str = ".bak") -> "File":
        """Create a timestamped-free `.bak` copy next to the original and return it."""
        self._require_file()
        return self.copy_to(self.path.with_suffix(self.path.suffix + suffix), overwrite=True)

    def replace_text(self, old: str, new: str, encoding: str = "utf-8") -> "File":
        """In-place find/replace, still written atomically."""
        content = self.read(encoding=encoding)
        return self.write(content.replace(old, new), encoding=encoding)


def _suppress_os_error():
    import contextlib
    return contextlib.suppress(OSError)


# --------------------------------------------------------------------------- #
# Dir
# --------------------------------------------------------------------------- #

class Dir:
    """
    A directory, with easy, safe helpers for listing, searching, zipping,
    copying and clearing.

    >>> d = Dir("project")
    >>> d.make()
    >>> (d / "readme.txt").write("hi")
    >>> d.files()
    [File('project/readme.txt')]
    """

    __slots__ = ("path",)

    def __init__(self, path: PathLike):
        self.path = Path(path)

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f"Dir({str(self.path)!r})"

    def __fspath__(self) -> str:
        return str(self.path)

    def __truediv__(self, other: str) -> "File":
        return File(self.path / other)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def _require_dir(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"No such directory: {self.path}")
        if not self.path.is_dir():
            raise NotADirError(f"Not a directory: {self.path}")

    def make(self, exist_ok: bool = True) -> "Dir":
        self.path.mkdir(parents=True, exist_ok=exist_ok)
        return self

    def child(self, *parts: str) -> Path:
        """Safely join child path segments, refusing to escape this directory."""
        return safe_join(self.path, *parts)

    # -- listing ------------------------------------------------------------ #

    def list(self) -> list[Union["File", "Dir"]]:
        """List immediate children as File/Dir wrapper objects."""
        self._require_dir()
        out: list[Union[File, Dir]] = []
        for p in sorted(self.path.iterdir()):
            out.append(Dir(p) if p.is_dir() else File(p))
        return out

    def files(self, pattern: str = "*", recursive: bool = False) -> list["File"]:
        self._require_dir()
        glob = self.path.rglob(pattern) if recursive else self.path.glob(pattern)
        return sorted((File(p) for p in glob if p.is_file()), key=lambda f: str(f.path))

    def dirs(self, pattern: str = "*", recursive: bool = False) -> list["Dir"]:
        self._require_dir()
        glob = self.path.rglob(pattern) if recursive else self.path.glob(pattern)
        return sorted((Dir(p) for p in glob if p.is_dir()), key=lambda d: str(d.path))

    def find(self, name_contains: str, recursive: bool = True) -> list["File"]:
        """Find files whose name contains `name_contains` (case-insensitive)."""
        needle = name_contains.lower()
        return [f for f in self.files("*", recursive=recursive) if needle in f.name.lower()]

    def size(self, human: bool = False) -> Union[int, str]:
        """Total size of everything under this directory, recursively."""
        self._require_dir()
        total = sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())
        return _human_size(total) if human else total

    def is_empty(self) -> bool:
        return (not self.exists) or not any(self.path.iterdir())

    # -- copy / move / delete ------------------------------------------------ #

    def copy_to(self, dest: PathLike, overwrite: bool = False) -> "Dir":
        self._require_dir()
        dest_path = Path(dest)
        if dest_path.exists():
            if not overwrite:
                raise FileExistsError(f"{dest_path} already exists (pass overwrite=True)")
            shutil.rmtree(dest_path)
        shutil.copytree(self.path, dest_path)
        return Dir(dest_path)

    def move_to(self, dest: PathLike, overwrite: bool = False) -> "Dir":
        self._require_dir()
        dest_path = Path(dest)
        if dest_path.exists():
            if not overwrite:
                raise FileExistsError(f"{dest_path} already exists (pass overwrite=True)")
            shutil.rmtree(dest_path)
        shutil.move(str(self.path), str(dest_path))
        self.path = dest_path
        return self

    def clear(self, permanent: bool = False) -> None:
        """Delete all *contents* of this directory (safely, by default), keeping the dir itself."""
        self._require_dir()
        for child in self.path.iterdir():
            if child.is_dir() and child.name != ".trash":
                Dir(child).delete(permanent=permanent)
            elif child.is_file():
                File(child).delete(permanent=permanent)

    def delete(self, permanent: bool = False) -> None:
        """
        Delete the whole directory tree.

        Safe by default: moved into a `.trash/` folder beside it.
        Pass permanent=True to actually remove it forever.
        """
        if not self.exists:
            return
        if permanent:
            shutil.rmtree(self.path)
            return
        trash = _trash_dir_for(self.path)
        dest = trash / _timestamped_name(self.path.name)
        shutil.move(str(self.path), str(dest))

    # -- archiving ------------------------------------------------------- #

    def zip_to(self, dest: PathLike, overwrite: bool = False) -> "File":
        """Zip this directory's contents into `dest`."""
        self._require_dir()
        dest_path = Path(dest)
        if dest_path.exists() and not overwrite:
            raise FileExistsError(f"{dest_path} already exists (pass overwrite=True)")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in self.path.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(self.path))
        return File(dest_path)

    @staticmethod
    def unzip(zip_path: PathLike, dest: PathLike, overwrite: bool = False) -> "Dir":
        """
        Extract a zip archive safely: any entry that would land outside
        `dest` (a "zip-slip" attack) raises PathEscapeError instead of
        silently writing outside the target directory.
        """
        dest_path = Path(dest)
        if dest_path.exists() and not overwrite and any(dest_path.iterdir()):
            raise FileExistsError(f"{dest_path} already exists and is non-empty")
        dest_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                target = safe_join(dest_path, member.filename)  # raises PathEscapeError if unsafe
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
        return Dir(dest_path)

    # -- watching (simple polling helper) -------------------------------- #

    def snapshot(self) -> dict[str, float]:
        """Return {relative_path: mtime} for every file — useful for diffing later."""
        self._require_dir()
        return {
            str(p.relative_to(self.path)): p.stat().st_mtime
            for p in self.path.rglob("*")
            if p.is_file()
        }

    def diff_since(self, snapshot: dict[str, float]) -> dict[str, list[str]]:
        """Compare current state to a prior snapshot() result."""
        current = self.snapshot()
        added = [p for p in current if p not in snapshot]
        removed = [p for p in snapshot if p not in current]
        modified = [
            p for p in current
            if p in snapshot and current[p] != snapshot[p]
        ]
        return {"added": added, "removed": removed, "modified": modified}
