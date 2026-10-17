"""On-disk form of SolveCache documents (Plan 17): `<key>.json.gz` (gzip, written by default;
PN_CACHE_GZIP=0 writes plain) or plain `<key>.json` (older entries, documents pulled from boxes
that wrote plain). Readers take either, the gz first when both exist. The key is of the request
(<key>.toml stays plain), so compressing never changes a key. Only files named by a sha256 key
are documents: the flop index, the registry index, lazydoc `.index.json` files and `.tmp` files
never are.

`compress` converts plain documents in place, crash-safe: write a temp file of its own
(`<key>.json.gz.<random>.tmp`: every writer has its own, so two never share an inode), fsync,
verify (the decompressed bytes equal the original), re-check that the plain file is the one it
read, rename over `<key>.json.gz`, then delete the plain file. A crash leaves the plain file, or
both complete forms (the next run deletes the plain one), never a partial document. It holds the
cache's solve lock (pn.solve.lock, as a background holder) for each file, so it never overlaps
SolveCache.solve's write or another compress run on the same file; a writer outside the lock (an
rsync pull) is caught by the re-check, or by the min-age rule."""

from __future__ import annotations

import errno
import gzip
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from pn.solve.lock import file_lock

PLAIN = ".json"
GZ = ".json.gz"
LEVEL = 6  # measured on 300 real flop docs: 16x smaller, json.load 1.08x plain; -9 gains 4% at 6x the time
MIN_AGE_SEC = 600  # compress skips plain files modified this recently (a pull may be writing them)
_DOC = re.compile(r"([0-9a-f]{64})\.json(\.gz)?")
# a writer's temp file: <key>.json[.gz].<random>.tmp, or the fixed <key>.json[.gz].tmp of older code
_TMP = re.compile(r"[0-9a-f]{64}\.json(\.gz)?(\.[^.]+)?\.tmp")


def gzip_enabled() -> bool:
    return os.environ.get("PN_CACHE_GZIP", "1") != "0"


def key_of(name: str) -> str | None:
    """The key a document file name carries (None: not a document)."""
    m = _DOC.fullmatch(name)
    return m.group(1) if m else None


def doc_path(root: str | Path, key: str) -> Path | None:
    """The existing document file of `key` (gz first), else None."""
    for suffix in (GZ, PLAIN):
        p = Path(root) / f"{key}{suffix}"
        if p.exists():
            return p
    return None


def write_path(root: str | Path, key: str) -> Path:
    return Path(root) / f"{key}{GZ if gzip_enabled() else PLAIN}"


def doc_files(root: str | Path) -> dict[str, Path]:
    """key -> its document file (gz first) for every document in `root`, in key order."""
    out: dict[str, Path] = {}
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return out
    for e in entries:
        key = key_of(e.name)
        if key is not None and (e.name.endswith(".gz") or key not in out):
            out[key] = Path(e.path)
    return dict(sorted(out.items()))


def open_binary(path: str | Path) -> IO[bytes]:
    """The decompressed byte stream of a document file of either form."""
    return gzip.open(path, "rb") if str(path).endswith(".gz") else open(path, "rb")


def load(path: str | Path) -> Any:
    with open_binary(path) as f:
        return json.load(f)


def read(root: str | Path, key: str) -> Any:
    """`key`'s document, None when it has none. Retries once when `pn cache compress` swaps the
    plain file for the gz between finding and opening it; a gz that does not read back (a torn
    write) falls back to a plain copy next to it, else the error propagates."""
    for _ in range(2):
        path = doc_path(root, key)
        if path is None:
            return None
        try:
            return load(path)
        except FileNotFoundError:
            continue
        except (OSError, EOFError, ValueError):
            plain = Path(root) / f"{key}{PLAIN}"
            if path.name.endswith(GZ) and plain.exists():
                return load(plain)
            raise
    return None


def _write_tmp(dest: Path, data: bytes, sync: bool) -> Path:
    """`data` in a new temp file next to `dest` that no other writer can be using."""
    fd, name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o644)  # mkstemp makes 0600; documents are 0644 like any other write
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if sync:
                f.flush()
                os.fsync(f.fileno())
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(name)
        raise
    return Path(name)


def dump(root: str | Path, key: str, doc: Any) -> Path:
    """Write `doc` under `key` atomically in the configured form and drop the other form (else a
    stale gz would shadow a newer plain document, or compress would revive a stale plain one).
    When the other form exists the new file is fsynced (file and directory) before the other is
    deleted, so a power loss cannot leave only a torn copy."""
    path = write_path(root, key)
    other = Path(root) / f"{key}{PLAIN if path.name.endswith('.gz') else GZ}"
    data = json.dumps(doc).encode()
    body = gzip.compress(data, LEVEL, mtime=0) if path.name.endswith(".gz") else data
    tmp = _write_tmp(path, body, sync=other.exists())
    os.replace(tmp, path)
    if other.exists():
        _fsync_dir(path.parent)
        with suppress(FileNotFoundError):
            os.unlink(other)
    return path


def remove(root: str | Path, key: str) -> list[Path]:
    """Delete both forms of `key`'s document; the paths removed."""
    gone = []
    for suffix in (GZ, PLAIN):
        p = Path(root) / f"{key}{suffix}"
        try:
            os.unlink(p)
            gone.append(p)
        except FileNotFoundError:
            pass
    return gone


# ------------------------------------------------------------------ compress


@dataclass
class CompressReport:
    converted: int = 0  # plain documents now gz (dry run: that would be)
    recent: int = 0  # skipped: modified within min_age_sec, or rewritten while being converted
    failed: int = 0  # skipped: not valid JSON, verification failed or an OSError (plain file kept)
    duplicates: int = 0  # plain copies dropped because an equal-or-newer gz exists
    vanished: int = 0  # skipped: the plain file went away (another writer won)
    stopped: bool = False  # the disk filled up: the run stopped early
    before_bytes: int = 0  # plain bytes converted
    after_bytes: int = 0  # gz bytes written


class _Changed(Exception):
    """The plain file (or its gz) changed while it was being converted."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ident(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _unchanged(plain: Path, st: os.stat_result) -> None:
    """FileNotFoundError when `plain` is gone, _Changed when it is another file than at `st`."""
    now = _ident(plain)
    if now is None:
        raise FileNotFoundError(errno.ENOENT, "vanished", str(plain))
    if now != (st.st_ino, st.st_mtime_ns, st.st_size):
        raise _Changed(plain.name)


def _convert(plain: Path, st: os.stat_result, level: int) -> int:
    """plain -> verified gz (keeping the plain file's mtime: it dates the solve); the gz size.
    ValueError when the plain file is not JSON or the gz does not read back equal; just before
    the swap, FileNotFoundError when the plain file is gone and _Changed when it or the gz is not
    what it was at `st`."""
    gz = plain.with_name(plain.name + ".gz")
    gz_before = _ident(gz)
    data = plain.read_bytes()
    json.loads(data)  # an unreadable document stays as it is
    tmp = _write_tmp(gz, gzip.compress(data, level, mtime=0), sync=True)
    try:
        if gzip.decompress(tmp.read_bytes()) != data:
            raise ValueError(f"{gz.name}: verification failed")
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        size = tmp.stat().st_size
        _unchanged(plain, st)
        if _ident(gz) != gz_before:
            raise _Changed(gz.name)
        os.replace(tmp, gz)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(plain.parent)
    os.unlink(plain)
    return size


def _sweep_tmp(root: Path, now: float, min_age_sec: float) -> None:
    """Old document temp files of a killed writer (a fresh one may be another writer's)."""
    for e in os.scandir(root):
        if _TMP.fullmatch(e.name):
            with suppress(FileNotFoundError):
                if now - e.stat().st_mtime >= min_age_sec:
                    os.unlink(e.path)


def _drop_duplicate(plain: Path, st: os.stat_result, gz: Path) -> bool:
    """Delete `plain` next to an equal-or-newer readable gz; False (both kept: a person decides)
    when the gz does not read back."""
    try:
        load(gz)
    except (OSError, ValueError, EOFError):
        return False
    _unchanged(plain, st)
    os.unlink(plain)
    return True


def _one(plain: Path, rep: CompressReport, now: float, dry_run: bool, min_age_sec: float,
         level: int) -> Path | None:  # fmt: skip
    """Process one plain document into `rep`; the gz written, if any."""
    try:
        st = plain.stat()
    except FileNotFoundError:
        rep.vanished += 1
        return None
    if now - st.st_mtime < min_age_sec:
        rep.recent += 1
        return None
    gz = plain.with_name(plain.name + ".gz")
    gz_st = _ident(gz)
    if gz_st is not None and gz_st[1] >= st.st_mtime_ns:
        if dry_run or _drop_duplicate(plain, st, gz):
            rep.duplicates += 1
        else:
            rep.failed += 1
        return None
    if dry_run:
        rep.converted += 1
        rep.before_bytes += st.st_size
        return None
    rep.after_bytes += _convert(plain, st, level)
    rep.converted += 1
    rep.before_bytes += st.st_size
    return gz


def compress(root: str | Path, *, dry_run: bool = False, limit: int | None = None,
             min_age_sec: float = MIN_AGE_SEC, level: int = LEVEL,
             on_file: Callable[[Path], None] | None = None) -> CompressReport:  # fmt: skip
    """Convert up to `limit` plain documents in `root` to gz (see the module doc). Idempotent and
    resumable. A plain file next to an equal-or-newer gz is a leftover copy and is deleted; next
    to an older gz it is newer content (a pull) and replaces the gz. A file that vanishes or
    changes mid-way is skipped; any other OSError fails that file (plain kept) and the run goes on,
    except a full disk, which stops it (`stopped`)."""
    root = Path(root)
    rep = CompressReport()
    now = time.time()
    if not dry_run:
        _sweep_tmp(root, now, min_age_sec)
    for e in sorted(os.scandir(root), key=lambda e: e.name):
        if limit is not None and rep.converted >= limit:
            break
        if not (e.name.endswith(PLAIN) and key_of(e.name)):
            continue
        plain = Path(e.path)
        try:
            if dry_run:
                gz = _one(plain, rep, now, dry_run, min_age_sec, level)
            else:
                with file_lock(root, background=True):
                    gz = _one(plain, rep, now, dry_run, min_age_sec, level)
        except FileNotFoundError:
            rep.vanished += 1  # deleted or replaced mid-way: another writer won
            continue
        except _Changed:
            rep.recent += 1  # rewritten mid-way: the next run converts the new content
            continue
        except ValueError:
            rep.failed += 1
            continue
        except OSError as err:
            rep.failed += 1
            if err.errno == errno.ENOSPC:
                rep.stopped = True
                break
            continue
        if gz is not None and on_file:
            on_file(gz)
    return rep
