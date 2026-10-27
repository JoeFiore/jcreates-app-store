"""Public mod index for the /mods download page.

Scans the server's mods folder, reads each jar's mod id (the registry
namespace), and remembers the mapping on disk.  On startup it does a full
scan; afterwards it quick-checks the folder for new/changed/removed jars.

Manual overrides live in mod_mappings.json and always win over
auto-detection: {namespace: filename}
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path


def _read_fabric_mod_id(jar: zipfile.ZipFile) -> dict | None:
    try:
        raw = jar.read("fabric.mod.json")
    except KeyError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    mod_id = data.get("id")
    if not mod_id:
        return None
    return {
        "id": mod_id,
        "name": data.get("name", mod_id),
        "version": str(data.get("version", "")),
    }


def _read_forge_mod_id(jar: zipfile.ZipFile) -> dict | None:
    # NeoForge/Forge mods.toml — minimal parse, no external deps
    try:
        raw = jar.read("META-INF/mods.toml").decode("utf-8", "replace")
    except KeyError:
        return None
    mod_id = display = version = None
    in_mods = False
    for line in raw.splitlines():
        s = line.strip()
        if s == "[[mods]]":
            in_mods = True
            continue
        if s.startswith("[") and s != "[[mods]]":
            in_mods = False
        if not in_mods or "=" not in s or s.startswith("#"):
            continue
        k, _, v = s.partition("=")
        v = v.strip().strip('"').strip("'")
        k = k.strip()
        if k == "modId" and mod_id is None:
            mod_id = v
        elif k == "displayName" and display is None:
            display = v
        elif k == "version" and version is None:
            version = v
    if not mod_id:
        return None
    return {"id": mod_id, "name": display or mod_id, "version": version or ""}


def identify_jar(path: Path) -> dict | None:
    """Return {id, name, version} for a mod jar, or None if unrecognized."""
    try:
        with zipfile.ZipFile(path) as jar:
            info = _read_fabric_mod_id(jar) or _read_forge_mod_id(jar)
    except (zipfile.BadZipFile, OSError):
        return None
    return info


class ModIndex:
    def __init__(self, server_dir: Path | str, data_dir: Path | str | None = None):
        self.mods_dir = Path(server_dir) / "mods"
        base = Path(data_dir) if data_dir else Path(server_dir)
        self.index_file = base / "mods_index.json"
        self.mappings_file = base / "mod_mappings.json"
        self._index: dict[str, dict] = {}
        self._manual: dict[str, str] = {}
        self._known_files: dict[str, float] = {}
        self.load()
        self.refresh(force=True)

    # ----- persistence -----
    def load(self) -> None:
        if self.index_file.exists():
            try:
                data = json.loads(self.index_file.read_text())
                self._index = data.get("mods", {})
                self._known_files = data.get("files", {})
            except (json.JSONDecodeError, OSError):
                pass
        if self.mappings_file.exists():
            try:
                self._manual = json.loads(self.mappings_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        try:
            self.index_file.write_text(json.dumps(
                {"mods": self._index, "files": self._known_files}, indent=2))
        except OSError:
            pass

    def save_mappings(self) -> None:
        try:
            self.mappings_file.write_text(json.dumps(self._manual, indent=2))
        except OSError:
            pass

    # ----- scanning -----
    def refresh(self, force: bool = False) -> dict:
        """Quick-check the mods folder; rescan new/changed jars only."""
        if not self.mods_dir.is_dir():
            return {"scanned": 0, "total": len(self._index)}
        current: dict[str, float] = {}
        for p in self.mods_dir.iterdir():
            if p.is_file() and p.suffix.lower() == ".jar":
                try:
                    current[p.name] = p.stat().st_mtime
                except OSError:
                    continue
        if not force and current == self._known_files:
            return {"scanned": 0, "total": len(self._index)}

        removed = set(self._known_files) - set(current)
        for name in removed:
            self._index = {k: v for k, v in self._index.items()
                           if v.get("file") != name}

        scanned = 0
        for name, mtime in current.items():
            if not force and self._known_files.get(name) == mtime:
                continue
            info = identify_jar(self.mods_dir / name)
            # drop stale entries for this file, then add fresh one
            self._index = {k: v for k, v in self._index.items()
                           if v.get("file") != name}
            if info:
                self._index[info["id"]] = {
                    "file": name,
                    "name": info["name"],
                    "version": info["version"],
                    "size": (self.mods_dir / name).stat().st_size,
                }
                scanned += 1
        self._known_files = current
        self.save()
        return {"scanned": scanned, "total": len(self._index)}

    # ----- lookups -----
    def lookup(self, namespace: str) -> dict | None:
        """Resolve a registry namespace to a downloadable mod, if known."""
        manual_file = self._manual.get(namespace)
        if manual_file:
            p = self.mods_dir / manual_file
            if p.is_file():
                return {"id": namespace, "name": namespace,
                        "version": "", "file": manual_file,
                        "size": p.stat().st_size, "manual": True}
            return None
        entry = self._index.get(namespace)
        if entry:
            return {**entry, "id": namespace, "manual": False}
        return None

    def file_path(self, namespace: str) -> Path | None:
        self.refresh()  # pick up jars added/removed/renamed while running
        entry = self.lookup(namespace)
        if not entry:
            return None
        p = self.mods_dir / entry["file"]
        return p if p.is_file() else None

    def check(self, namespaces: list[str]) -> dict:
        self.refresh()  # keep check and download in agreement
        available, missing = [], []
        for ns in namespaces:
            (available if self.lookup(ns) else missing).append(ns)
        return {"available": available, "missing": missing}

    def list_all(self) -> list[dict]:
        self.refresh()
        out = []
        for mod_id in sorted(self._index):
            e = self._index[mod_id]
            out.append({"id": mod_id, "name": e["name"],
                        "version": e["version"], "file": e["file"],
                        "size": e["size"], "manual": False})
        for ns in sorted(self._manual):
            if ns not in self._index:
                entry = self.lookup(ns)
                if entry:
                    out.append({**entry, "manual": True})
        return out

    # ----- manual mappings -----
    def set_mapping(self, namespace: str, filename: str) -> None:
        self._manual[namespace] = filename
        self.save_mappings()

    def remove_mapping(self, namespace: str) -> bool:
        if namespace in self._manual:
            del self._manual[namespace]
            self.save_mappings()
            return True
        return False

    def get_mappings(self) -> dict[str, str]:
        return dict(self._manual)
