from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

FILES = ("clause-resource-kb.json", "runtime-tool-resource-kb.json", "clause-lattice-time-kb.json")
OPTIONAL_FILES = ("edge-kappa-kb.json",)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def user_state_dir() -> Path:
    configured = os.getenv("CLAWTUNE_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    # Resolve the invoking user, not root's HOME after sudo.
    if os.name == "posix" and os.getenv("SUDO_USER"):
        import pwd
        home = Path(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        return home / ".local/state/clawtune"
    return Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "clawtune"


def validate_seed(path: Path) -> dict:
    path = path.resolve(strict=True)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    from .contracts import validate
    validate(manifest, "kb-seed.schema.json")
    if manifest.get("schema") != "clawtune.seed.v1" or not set(FILES) <= set(manifest.get("snapshots", {})) <= set(FILES + OPTIONAL_FILES):
        raise ValueError(f"invalid seed bundle: {path}")
    for name in manifest["snapshots"]:
        target = (path / name).resolve(strict=True)
        if not target.is_relative_to(path) or digest(target) != manifest["snapshots"][name]:
            raise ValueError(f"seed snapshot hash mismatch: {name}")
        obj = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(obj, dict) or not isinstance(obj.get("schema"), str):
            raise ValueError(f"invalid KB snapshot: {name}")
        if obj.get("pending"):
            raise ValueError(f"seed must have finalized observations: {name}")
    return manifest


def create_seed(path: Path, payloads: dict[str, dict], *, provenance: dict) -> dict:
    if not set(FILES) <= set(payloads) <= set(FILES + OPTIONAL_FILES):
        raise ValueError("a seed must contain the three legacy KBs and may include EdgeKappaKB")
    path.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        write_json(path / name, payload)
    result = {"schema": "clawtune.seed.v1", "provenance": provenance,
              "snapshots": {name: digest(path / name) for name in payloads}}
    write_json(path / "manifest.json", result)
    validate_seed(path)
    return result


def initialize_state(path: Path, seed: Path, *, owner: str) -> None:
    seed = seed.resolve(strict=True)
    manifest = validate_seed(seed)
    path = path.resolve()
    if path == seed or path.is_relative_to(seed):
        raise ValueError("writable state must be outside the seed")
    if path.exists():
        raise FileExistsError(f"state already exists; resume it explicitly: {path}")
    path.mkdir(parents=True)
    for name in manifest["snapshots"]:
        shutil.copyfile(seed / name, path / name)
    write_json(path / "state.json", {"schema": "clawtune.kb-state.v1", "owner": owner,
               "seed_sha256": digest(seed / "manifest.json"), "generation": 0,
               "snapshots": manifest["snapshots"]})
    with StateStore(path) as store:
        store.checkpoint()


def committed_state(path: Path) -> dict:
    """Read the atomic commit, not the mutable compatibility working set."""
    name = (path / "CURRENT").read_text(encoding="ascii").strip()
    if not name.isdigit():
        raise ValueError("invalid KB CURRENT generation")
    folder = path / "generations" / name
    if folder.is_symlink() or folder.resolve().parent != (path / "generations").resolve():
        raise ValueError("invalid KB generation directory")
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    from .contracts import validate
    validate(state, "kb-state.schema.json")
    for filename in state["snapshots"]:
        if digest(folder / filename) != state["snapshots"].get(filename):
            raise ValueError(f"corrupt committed KB generation: {filename}")
    return state


class StateStore:
    """One writer; CURRENT is the atomic commit point for all registered snapshots.

    Root snapshots are a compatibility working set. Only generation snapshots
    are authoritative after a crash. Interrupted uncommitted updates are lost.
    """

    def __init__(self, path: Path):
        self.path = path.resolve(strict=True)
        self._lock = None

    def __enter__(self):
        self._lock = (self.path / ".writer.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock.seek(0)
                if not self._lock.read(1):
                    self._lock.write(b"0")
                    self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.restore()
        except BaseException:
            self._lock.close()
            self._lock = None
            raise
        return self

    def __exit__(self, *args):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def restore(self) -> None:
        pointer = self.path / "CURRENT"
        if not pointer.exists():
            return
        name = pointer.read_text(encoding="ascii").strip()
        if not name.isdigit():
            raise ValueError("invalid KB CURRENT generation")
        folder = self.path / "generations" / name
        committed = committed_state(self.path)
        for filename in OPTIONAL_FILES:
            if filename not in committed["snapshots"]:
                (self.path / filename).unlink(missing_ok=True)
        for filename in (*committed["snapshots"], "state.json"):
            shutil.copyfile(folder / filename, self.path / filename)

    def checkpoint(self) -> dict:
        if self._lock is None:
            raise RuntimeError("KB checkpoint requires its writer lock")
        state = json.loads((self.path / "state.json").read_text(encoding="utf-8"))
        names = (*FILES, *(name for name in OPTIONAL_FILES if (self.path / name).is_file()))
        hashes = {name: digest(self.path / name) for name in names}
        if (self.path / "CURRENT").exists() and hashes == state["snapshots"]:
            return state
        # An interrupted commit may have left a directory without CURRENT.
        generation = state["generation"] + 1
        while (self.path / "generations" / str(generation)).exists():
            generation += 1
        folder = self.path / "generations" / str(generation)
        folder.mkdir(parents=True)
        for name in names:
            shutil.copyfile(self.path / name, folder / name)
        state = {**state, "generation": generation, "snapshots": hashes}
        write_json(folder / "state.json", state)
        temp = self.path / ".CURRENT.tmp"
        with temp.open("w", encoding="ascii") as stream:
            stream.write(str(generation))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path / "CURRENT")
        write_json(self.path / "state.json", state)
        # Bound storage to current + previous committed snapshots. Targets are
        # verified child directories; never follow a link during cleanup.
        generations = self.path / "generations"
        candidates = sorted((p for p in generations.iterdir() if p.name.isdigit() and not p.is_symlink()),
                            key=lambda p: int(p.name), reverse=True)
        for stale in candidates[2:]:
            if stale.resolve().parent == generations.resolve():
                shutil.rmtree(stale)
        return state
