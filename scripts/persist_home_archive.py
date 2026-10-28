#!/usr/bin/env python3
"""Recover known home mappings from docker-cp tar streams without following links."""

import os
import json
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

DATA_ROOT = Path("/data")
MAPPINGS = {
    "workspace": ("workspace", True),
    "downloads": ("browser/downloads", True),
    ".claude": (".claude", True),
    ".codex": (".codex", True),
    ".vscode-server": (".vscode-server", True),
    ".ssh": ("ssh", True),
    ".agents": ("agents", True),
    ".config": ("home-config/.config", True),
    ".claude.json": ("home-config/claude.json", False),
    ".bashrc.extra": ("home-config/bashrc.extra", False),
    ".gitconfig": ("home-config/gitconfig", False),
    ".npmrc": ("home-config/npmrc", False),
}


def member_path(name, source):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or ".." in parts or parts[0] != source:
        raise ValueError("archive path is outside the requested mapping")
    return Path(*parts[1:])


def ensure_directory(path, root):
    """Reject existing symlink components before writing anything beneath them."""
    relative = path.relative_to(root)
    current = root
    for part in ("", *relative.parts):
        if part:
            current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            continue
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise ValueError("destination parent is not a real directory")


def leaf_is_safe(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise ValueError("destination leaf is not a regular file")


def metadata(path, member, *, symlink=False):
    if member.uid < 0 or member.gid < 0:
        raise ValueError("invalid archive owner")
    os.chown(path, member.uid, member.gid, follow_symlinks=not symlink)
    if not symlink:
        os.chmod(path, stat.S_IMODE(member.mode))
        os.utime(path, (member.mtime, member.mtime))


def persist_file(archive, member, path):
    leaf_is_safe(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".ai-dev-recover-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("archive file has no data")
            with source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        metadata(temporary, member)
        leaf_is_safe(path)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def mapped_link(source, relative, link):
    """Keep link semantics across the different logical-home and volume paths."""
    if not link:
        raise ValueError("empty archive link")
    # Lexical normalization may not cancel a component followed by '..': that
    # component could itself be a symlink. Only leading relative parents can be
    # mapped without resolving and reading another part of the legacy home.
    component_seen = False
    for component in link.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if component_seen or posixpath.isabs(link):
                raise ValueError("cannot safely map a parent after a link component")
        else:
            component_seen = True
    logical_parent = posixpath.join("/home/dev", source, relative.parent.as_posix())
    logical_target = posixpath.normpath(posixpath.join(logical_parent, link))
    for name, (destination, is_directory) in MAPPINGS.items():
        for prefix in ("/home/dev/" + name, "/home/dev/.ai-data/" + destination):
            if logical_target == prefix:
                suffix = ""
            elif is_directory and logical_target.startswith(prefix + "/"):
                suffix = logical_target[len(prefix) + 1:]
            else:
                continue
            mapped_target = posixpath.join(destination, suffix).rstrip("/")
            if posixpath.isabs(link):
                # Preserve canonical absolute home links; remove harmless dot
                # components only after ambiguous parent traversal is rejected.
                return link if link == logical_target else "/home/dev/.ai-data/" + mapped_target
            mapped_parent = posixpath.join(MAPPINGS[source][0], relative.parent.as_posix())
            return posixpath.relpath(mapped_target, mapped_parent)
    raise ValueError("archive link targets an undeclared home mapping")


def apply_archive(stream, source, *, verify=False, root=DATA_ROOT, before_write=None):
    if source not in MAPPINGS:
        raise ValueError("unknown home mapping")
    destination, is_directory = MAPPINGS[source]
    target = root / destination
    directories = []
    hardlinks = []
    regular = set()
    seen = set()
    root_member = None
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            relative = member_path(member.name, source)
            key = relative.as_posix()
            if key in seen:
                raise ValueError("duplicate archive member")
            seen.add(key)
            if root_member is None:
                if key != ".":
                    raise ValueError("archive does not start at the requested root")
                root_member = member
                if verify:
                    if source != "workspace" or not member.issym() or member.linkname != "/home/dev/.ai-data/workspace":
                        raise ValueError("legacy home is not initialized")
                elif member.issym():
                    expected = "/home/dev/.ai-data/" + destination
                    link = member.linkname
                    # Only direct volume paths are trusted. Do not normalize a
                    # component followed by '..', which may itself be a link.
                    if not link or ".." in link.split("/") or posixpath.normpath(
                        posixpath.join("/home/dev", link)) != expected:
                        raise ValueError("root link does not address its declared volume mapping")
                elif not member.issym():
                    if (is_directory and not member.isdir()) or (not is_directory and not member.isfile()):
                        raise ValueError("unexpected mapping type")
                    if before_write is not None:
                        before_write()
            elif verify or root_member.issym() or not is_directory:
                raise ValueError("unexpected entries after a root link or file")
            if verify or root_member.issym():
                # Existing root symlinks already address the volume. Never read
                # their target; the new entrypoint will assert the canonical link.
                continue
            path = target / relative
            if member.isdir():
                ensure_directory(path, root)
                directories.append((path, member))
                continue
            ensure_directory(path.parent, root)
            if member.isfile():
                persist_file(archive, member, path)
                regular.add(key)
            elif member.issym():
                # Store declared home links without following them. Relative
                # cross-mapping links need the equivalent volume-relative path.
                link = mapped_link(source, relative, member.linkname)
                if os.path.lexists(path):
                    mode = path.lstat().st_mode
                    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                        raise ValueError("cannot replace destination directory with link")
                    path.unlink()
                os.symlink(link, path)
                metadata(path, member, symlink=True)
            elif member.islnk():
                linked = member_path(member.linkname, source)
                hardlinks.append((path, linked.as_posix()))
            else:
                raise ValueError("special archive members are not supported")
    if root_member is None:
        raise ValueError("empty archive")
    # Docker emits hardlinks after their data member. Require the referenced
    # regular data to come from this stream, not from a preexisting volume file.
    for path, linked in hardlinks:
        if linked not in regular:
            raise ValueError("hardlink data is not present in this archive")
        original = target / linked
        ensure_directory(original.parent, root)
        leaf_is_safe(original)
        ensure_directory(path.parent, root)
        leaf_is_safe(path)
        if path.exists():
            path.unlink()
        os.link(original, path, follow_symlinks=False)
        regular.add(path.relative_to(target).as_posix())
    for path, member in reversed(directories):
        ensure_directory(path, root)
        metadata(path, member)
    return not verify and not root_member.issym()


def remove_path(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def transaction_dir(root):
    return root / ".ai-dev-recovery"


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def check_parent(path, root):
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise ValueError("destination parent is not a real directory")


def reject_external_hardlinks(path):
    """A staged copy cannot preserve hardlinks to files outside this mapping."""
    counts = {}
    links = {}
    candidates = [path]
    if path.is_dir():
        candidates = []
        for directory, _, files in os.walk(path, followlinks=False):
            candidates.extend(Path(directory) / name for name in files)
    for candidate in candidates:
        info = candidate.lstat()
        if stat.S_ISREG(info.st_mode):
            key = (info.st_dev, info.st_ino)
            counts[key] = counts.get(key, 0) + 1
            links[key] = info.st_nlink
    if any(links[key] > count for key, count in counts.items()):
        raise ValueError("destination has hardlinks outside its mapping")


def recover_transaction(root):
    tx = transaction_dir(root)
    if not os.path.lexists(tx):
        return
    if tx.is_symlink() or not tx.is_dir():
        raise ValueError("invalid recovery transaction directory")
    journal = tx / "journal.json"
    committed = tx / "committed"
    if journal.exists() and not committed.exists():
        data = json.loads(journal.read_text(encoding="utf-8"))
        for item in reversed(data["items"]):
            name = item["name"]
            if name not in MAPPINGS:
                raise ValueError("invalid transaction mapping")
            target = root / MAPPINGS[name][0]
            staged = tx / "stage" / MAPPINGS[name][0]
            backup = tx / "backup" / name
            check_parent(target.parent, root)
            if os.path.lexists(backup):
                if os.path.lexists(target):
                    remove_path(target)
                os.replace(backup, target)
                sync_directory(target.parent)
            elif not item["existed"] and not os.path.lexists(staged) and os.path.lexists(target):
                remove_path(target)
                sync_directory(target.parent)
        for relative in reversed(data["parents"]):
            directory = root / relative
            check_parent(directory, root)
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
    # Cleanup itself may be interrupted. Once rollback or commit is complete,
    # remove the journal first so a partially removed transaction can never be
    # mistaken for an unfinished commit on the next run.
    if journal.exists():
        journal.unlink()
        sync_directory(tx)
    shutil.rmtree(tx)
    sync_directory(root)


def begin_transaction(root):
    recover_transaction(root)
    tx = transaction_dir(root)
    tx.mkdir(mode=0o700)
    (tx / "stage").mkdir()
    (tx / "backup").mkdir()
    sync_directory(root)


def stage_archive(stream, source, root):
    if source not in MAPPINGS:
        raise ValueError("unknown home mapping")
    tx = transaction_dir(root)
    stage = tx / "stage"
    if not stage.is_dir() or stage.is_symlink():
        raise ValueError("missing recovery transaction")
    destination, is_directory = MAPPINGS[source]
    original, target = root / destination, stage / destination
    check_parent(original.parent, root)
    if os.path.lexists(target):
        raise ValueError("mapping already staged")
    def prepare_target():
        ensure_directory(target.parent, stage)
        if os.path.lexists(original):
            if original.is_symlink() or (is_directory and not original.is_dir()) or (not is_directory and not original.is_file()):
                raise ValueError("destination mapping has an unexpected type")
            reject_external_hardlinks(original)
            subprocess.run(["cp", "-a", "--", str(original), str(target)], check=True)
    try:
        apply_archive(stream, source, root=stage, before_write=prepare_target)
    except BaseException:
        if os.path.lexists(target):
            remove_path(target)
        raise


def commit_transaction(root):
    tx = transaction_dir(root)
    stage = tx / "stage"
    if not stage.is_dir() or stage.is_symlink():
        raise ValueError("missing recovery transaction")
    items = []
    parents = set()
    for name, (destination, _) in MAPPINGS.items():
        staged, target = stage / destination, root / destination
        if not os.path.lexists(staged):
            continue
        check_parent(target.parent, root)
        parent = target.parent
        while parent != root and not parent.exists():
            parents.add(parent.relative_to(root).as_posix())
            parent = parent.parent
        items.append({"name": name, "existed": os.path.lexists(target)})
    data = {"items": items, "parents": sorted(parents, key=lambda p: (p.count("/"), p))}
    write_json(tx / "journal.json", data)
    try:
        for relative in data["parents"]:
            (root / relative).mkdir()
            sync_directory((root / relative).parent)
        for item in items:
            name = item["name"]
            destination = MAPPINGS[name][0]
            target, staged, backup = root / destination, stage / destination, tx / "backup" / name
            check_parent(target.parent, root)
            if item["existed"]:
                os.replace(target, backup)
                sync_directory(target.parent)
                sync_directory(backup.parent)
            os.replace(staged, target)
            sync_directory(target.parent)
            sync_directory(staged.parent)
        write_json(tx / "committed", True)
    except BaseException:
        recover_transaction(root)
        raise
    recover_transaction(root)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("verify", "stage", "begin", "commit", "abort"):
        return 2
    try:
        mode, name = sys.argv[1:]
        if mode == "verify":
            apply_archive(sys.stdin.buffer, name, verify=True)
        elif mode == "stage":
            stage_archive(sys.stdin.buffer, name, DATA_ROOT)
        elif mode == "begin":
            begin_transaction(DATA_ROOT)
        elif mode == "commit":
            commit_transaction(DATA_ROOT)
        else:
            recover_transaction(DATA_ROOT)
    except tarfile.ReadError:
        # The caller distinguishes a missing source from a Docker/stream error.
        return 44
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError):
        print("offline recovery rejected the archive; source container retained", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
