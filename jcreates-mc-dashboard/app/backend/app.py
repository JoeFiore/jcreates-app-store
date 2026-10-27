"""MC Dashboard backend — FastAPI app.

Run:  DASHBOARD_PASSWORD=secret SERVER_DIR=/path/to/server \
      python -m backend.app        (from the project root)
or:   uvicorn backend.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException, Query,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .manager import ServerManager

try:
    from .mods import ModIndex
except ImportError:  # Docker build ships without ModLink
    ModIndex = None

# ---------- config ----------
BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"


def load_config() -> dict:
    cfg: dict = {}
    cfg_file = BASE / "config.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text())
    get = lambda k, default=None: os.environ.get(k.upper(), cfg.get(k, default))
    return {
        "server_dir": get("server_dir", str(BASE / "server")),
        "jar_file": get("jar_file", "server.jar"),
        "java_args": get("java_args", "-Xmx2G -Xms1G"),
        "password": get("dashboard_password", "changeme"),
        "port": int(get("port", 8000)),
        "rcon_host": get("rcon_host", "127.0.0.1"),
        "rcon_port": int(get("rcon_port", 25575)),
        "rcon_password": get("rcon_password", ""),
        # ModLink / public /mods hosting. The Docker build sets this to
        # false (see docker-compose.yml) — with it off, no mod routes are
        # registered at all in that build (backend/docker_app.py).
        "mods_enabled": str(get("mods_enabled", "true")).lower() not in
                        ("false", "0", "no", "off"),
    }


def _detect_jar(server_dir: str, preferred: str) -> str:
    """If the configured jar is missing, pick the obvious server jar.

    Prefers server.jar, then fabric-server-launch.jar, then the only jar
    in the server root. Returns the preferred name unchanged if nothing
    better is found, so the existing 'jar not found' error still fires.
    """
    root = Path(server_dir)
    if (root / preferred).exists():
        return preferred
    for name in ("server.jar", "fabric-server-launch.jar"):
        if (root / name).exists():
            return name
    jars = sorted(p.name for p in root.glob("*.jar") if p.is_file())
    return jars[0] if len(jars) == 1 else preferred


CONFIG = load_config()
CONFIG["jar_file"] = _detect_jar(CONFIG["server_dir"], CONFIG["jar_file"])
manager = ServerManager(CONFIG["server_dir"], CONFIG["jar_file"],
                        CONFIG["java_args"],
                        rcon_host=CONFIG["rcon_host"],
                        rcon_port=CONFIG["rcon_port"],
                        rcon_password=CONFIG["rcon_password"])
tokens: set[str] = set()

app = FastAPI(title="MC Dashboard")


# ---------- auth ----------
class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody):
    if not secrets.compare_digest(body.password, CONFIG["password"]):
        raise HTTPException(401, "wrong password")
    token = secrets.token_hex(16)
    tokens.add(token)
    return {"token": token}


def require_auth(authorization: str = Header("")):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token not in tokens:
        raise HTTPException(401, "unauthorized")
    return token


@app.post("/api/logout")
def logout(authorization: str = Header("")):
    _, _, token = authorization.partition(" ")
    tokens.discard(token)
    return {"ok": True}


# ---------- server control ----------
@app.get("/api/server", dependencies=[Depends(require_auth)])
def server_info():
    return manager.info()


@app.post("/api/server/start", dependencies=[Depends(require_auth)])
def server_start():
    return manager.start()


@app.post("/api/server/stop", dependencies=[Depends(require_auth)])
def server_stop():
    return manager.stop()


@app.post("/api/server/restart", dependencies=[Depends(require_auth)])
def server_restart():
    return manager.restart()


@app.post("/api/server/takeover", dependencies=[Depends(require_auth)])
def server_takeover():
    """Force-stop an externally-running server and restart it managed."""
    return manager.takeover()


@app.get("/api/config", dependencies=[Depends(require_auth)])
def get_config():
    return {"mods_enabled": CONFIG["mods_enabled"],
            "jar": manager.jar_file,
            "server_dir": str(manager.server_dir),
            "port": CONFIG["port"]}


class JarBody(BaseModel):
    jar_file: str


@app.post("/api/jar", dependencies=[Depends(require_auth)])
def set_jar(body: JarBody):
    name = (body.jar_file or "").strip()
    if not name or "/" in name or "\\" in name or not name.endswith(".jar"):
        raise HTTPException(400, "invalid jar name")
    if not (manager.server_dir / name).is_file():
        raise HTTPException(404, f"jar not found in server folder: {name}")
    manager.jar_file = name
    return {"ok": True, "jar": name}


@app.post("/api/eula", dependencies=[Depends(require_auth)])
def accept_eula():
    (manager.server_dir / "eula.txt").write_text(
        "#By changing the setting below to TRUE you are indicating your agreement to our EULA (https://aka.ms/MinecraftEULA).\n"
        "eula=true\n", encoding="utf-8")
    return {"ok": True}


def _safe_extract(zip_path: Path, dest: Path) -> int:
    """Extract a zip into dest, rejecting zip-slip paths and __MACOSX junk.
    Returns the number of files extracted."""
    count = 0
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            if member.is_dir():
                continue
            name = member.filename
            if name.startswith("__MACOSX/") or "/__MACOSX/" in name:
                continue
            target = (dest / name).resolve()
            if target != dest and dest not in target.parents:
                raise HTTPException(400, f"zip entry escapes the server folder: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


@app.post("/api/import", dependencies=[Depends(require_auth)])
async def import_zip(file: UploadFile = File(...), clean: bool = Form(True)):
    """Import a whole server from a zip: stop the server, extract the zip
    into the server folder, flatten a single top-level folder if the zip
    has one, pick the server jar, and report whether the EULA is still
    needed. With clean=True (the default) the old server files are
    removed first, except backups/ which are kept."""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "upload a .zip file")
    if manager.status == "running":
        r = manager.stop()
        if not r["ok"]:
            return r
        for _ in range(100):
            if manager.proc is None:
                break
            import time
            time.sleep(0.1)
    server_dir = manager.server_dir
    server_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = server_dir.parent / f".import-{secrets.token_hex(6)}.zip"
    staging = server_dir.parent / f".import-{secrets.token_hex(6)}"
    try:
        with tmp_zip.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
        staging.mkdir(parents=True, exist_ok=True)
        count = _safe_extract(tmp_zip, staging)
        if count == 0:
            raise HTTPException(400, "zip contained no files")
        # Flatten: zip with one top-level folder holding the server.
        entries = [p for p in staging.iterdir()]
        source = staging
        if len(entries) == 1 and entries[0].is_dir():
            inner = entries[0]
            if (inner / "server.properties").exists() or list(inner.glob("*.jar")):
                source = inner
        if clean:
            for child in server_dir.iterdir():
                if child.name == "backups":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        for child in source.iterdir():
            dest = server_dir / child.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(child), str(dest))
        jar = _detect_jar(str(server_dir), manager.jar_file)
        manager.jar_file = jar
        eula = server_dir / "eula.txt"
        eula_required = True
        if eula.exists():
            eula_required = "eula=true" not in eula.read_text(errors="replace")
        jars = sorted(p.name for p in server_dir.glob("*.jar") if p.is_file())
        return {"ok": True, "files": count, "jar": jar, "jars": jars,
                "eula_required": eula_required,
                "server_dir": str(server_dir)}
    finally:
        tmp_zip.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


class CommandBody(BaseModel):
    command: str


@app.post("/api/console", dependencies=[Depends(require_auth)])
def send_console(body: CommandBody):
    return manager.send_command(body.command)


@app.websocket("/ws/console")
async def console_ws(ws: WebSocket):
    await ws.accept()
    # simple token check via first message; reject explicitly before closing
    # so the client deterministically sees the rejection
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        hello = {}
    if hello.get("token") not in tokens:
        try:
            await ws.send_json({"type": "error", "error": "unauthorized"})
        finally:
            await ws.close(code=4401)
        return
    # send current state right away
    await ws.send_json({"type": "status", "status": manager.status,
                        "managed": manager.managed,
                        "rcon": manager.rcon_configured,
                        "players": sorted(manager.players)})
    q = manager.subscribe()
    try:
        while True:
            msg = await asyncio.to_thread(q.get)
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe(q)


# ---------- files ----------
def _resolve(rel: str) -> Path:
    """Resolve a user-supplied path, jailed inside the server dir."""
    base = manager.server_dir
    p = (base / (rel or "")).resolve()
    if p != base and base not in p.parents:
        raise HTTPException(403, "path escapes server directory")
    return p


TEXT_EXTS = {".txt", ".properties", ".json", ".yml", ".yaml", ".log",
             ".mcfunction", ".json5", ".cfg", ".toml"}


@app.get("/api/files", dependencies=[Depends(require_auth)])
def list_files(path: str = ""):
    p = _resolve(path)
    if not p.exists():
        raise HTTPException(404, "not found")
    if p.is_file():
        raise HTTPException(400, "path is a file")
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        st = child.stat()
        items.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "size": st.st_size if child.is_file() else None,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "editable": child.is_file() and child.suffix.lower() in TEXT_EXTS,
        })
    return {"path": path, "items": items}


@app.get("/api/files/read", dependencies=[Depends(require_auth)])
def read_file(path: str):
    p = _resolve(path)
    if not p.is_file():
        raise HTTPException(404, "not a file")
    if p.stat().st_size > 2_000_000:
        raise HTTPException(400, "file too large to edit in browser")
    try:
        return {"path": path, "content": p.read_text(encoding="utf-8", errors="replace")}
    except OSError as e:
        raise HTTPException(400, str(e))


class WriteBody(BaseModel):
    path: str
    content: str


@app.post("/api/files/write", dependencies=[Depends(require_auth)])
def write_file(body: WriteBody):
    p = _resolve(body.path)
    if p.is_dir():
        raise HTTPException(400, "path is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": body.path}


class PathBody(BaseModel):
    path: str


@app.post("/api/files/delete", dependencies=[Depends(require_auth)])
def delete_path(body: PathBody):
    p = _resolve(body.path)
    if p == manager.server_dir:
        raise HTTPException(400, "cannot delete the server root")
    if not p.exists():
        raise HTTPException(404, "not found")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"ok": True}


@app.post("/api/files/mkdir", dependencies=[Depends(require_auth)])
def make_dir(body: PathBody):
    p = _resolve(body.path)
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": body.path}


@app.post("/api/files/upload", dependencies=[Depends(require_auth)])
async def upload_file(path: str = "", file: UploadFile = File(...)):
    dest_dir = _resolve(path)
    if not dest_dir.is_dir():
        raise HTTPException(400, "upload target must be a directory")
    dest = _resolve(str(Path(path) / (file.filename or "upload.bin")))
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"ok": True, "path": str(dest.relative_to(manager.server_dir))}


# ---------- players ----------
@app.get("/api/players", dependencies=[Depends(require_auth)])
def get_players():
    manager.refresh_players()  # refresh from the server
    return {"players": sorted(manager.players)}


class PlayerBody(BaseModel):
    player: str
    reason: str = ""


def _player_cmd(cmd: str, player: str, reason: str = "") -> dict:
    if not player or not player.replace("_", "").isalnum():
        return {"ok": False, "error": "invalid player name"}
    full = f"{cmd} {player}" + (f" {reason}" if reason else "")
    return manager.send_command(full)


@app.post("/api/players/kick", dependencies=[Depends(require_auth)])
def kick(b: PlayerBody):
    return _player_cmd("kick", b.player, b.reason)


@app.post("/api/players/ban", dependencies=[Depends(require_auth)])
def ban(b: PlayerBody):
    return _player_cmd("ban", b.player, b.reason)


@app.post("/api/players/pardon", dependencies=[Depends(require_auth)])
def pardon(b: PlayerBody):
    return _player_cmd("pardon", b.player)


@app.post("/api/players/op", dependencies=[Depends(require_auth)])
def op(b: PlayerBody):
    return _player_cmd("op", b.player)


@app.post("/api/players/deop", dependencies=[Depends(require_auth)])
def deop(b: PlayerBody):
    return _player_cmd("deop", b.player)


@app.post("/api/players/whitelist-add", dependencies=[Depends(require_auth)])
def wl_add(b: PlayerBody):
    return _player_cmd("whitelist add", b.player)


@app.post("/api/players/whitelist-remove", dependencies=[Depends(require_auth)])
def wl_remove(b: PlayerBody):
    return _player_cmd("whitelist remove", b.player)


# ---------- backups ----------
def _world_dirs() -> list[Path]:
    level = "world"
    props = manager.server_dir / "server.properties"
    if props.exists():
        for line in props.read_text(errors="replace").splitlines():
            if line.startswith("level-name="):
                level = line.split("=", 1)[1].strip() or "world"
    return [d for d in manager.server_dir.iterdir()
            if d.is_dir() and (d.name == level or d.name.startswith(level + "_"))]


def _backups_dir() -> Path:
    d = manager.server_dir / "backups"
    d.mkdir(exist_ok=True)
    return d


@app.get("/api/backups", dependencies=[Depends(require_auth)])
def list_backups():
    items = []
    for z in sorted(_backups_dir().glob("*.zip"), reverse=True):
        st = z.stat()
        items.append({"name": z.name, "size": st.st_size,
                      "modified": datetime.fromtimestamp(st.st_mtime).isoformat()})
    return {"backups": items, "world_dirs": [d.name for d in _world_dirs()]}


@app.post("/api/backups", dependencies=[Depends(require_auth)])
def create_backup():
    worlds = _world_dirs()
    if not worlds:
        raise HTTPException(400, "no world folders found")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _backups_dir() / f"backup-{stamp}.zip"
    # quiet the world saves first so the zip is consistent
    manager.send_command("save-off")
    manager.send_command("save-all flush")
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for wdir in worlds:
                for f in wdir.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(manager.server_dir))
    finally:
        manager.send_command("save-on")
    st = dest.stat()
    return {"ok": True, "name": dest.name, "size": st.st_size}


@app.post("/api/backups/{name}/restore", dependencies=[Depends(require_auth)])
def restore_backup(name: str):
    if "/" in name or "\\" in name or not name.endswith(".zip"):
        raise HTTPException(400, "invalid backup name")
    src = _backups_dir() / name
    if not src.exists():
        raise HTTPException(404, "backup not found")
    if manager.status == "running":
        r = manager.stop()
        if not r["ok"]:
            return r
        for _ in range(100):  # wait for the process to actually exit
            if manager.proc is None:
                break
            import time
            time.sleep(0.1)
    # remove current world dirs, then unzip
    for wdir in _world_dirs():
        shutil.rmtree(wdir)
    with zipfile.ZipFile(src) as z:
        z.extractall(manager.server_dir)
    return {"ok": True, "restored": name}


@app.post("/api/backups/{name}/delete", dependencies=[Depends(require_auth)])
def delete_backup(name: str):
    if "/" in name or "\\" in name or not name.endswith(".zip"):
        raise HTTPException(400, "invalid backup name")
    src = _backups_dir() / name
    if not src.exists():
        raise HTTPException(404, "backup not found")
    src.unlink()
    return {"ok": True}


# ---------- logs ----------
def _tail_lines(p: Path, n: int) -> str:
    """Return the last n lines of a (possibly large) log file efficiently."""
    opener = open
    if p.suffix == ".gz":
        import gzip
        opener = gzip.open
    with opener(p, "rb") as f:  # type: ignore[call-arg]
        if p.suffix == ".gz":
            text = f.read().decode("utf-8", errors="replace")
            return "\n".join(text.split("\n")[-n:])
        f.seek(0, 2)
        pos = f.tell()
        buf = b""
        while pos > 0 and buf.count(b"\n") <= n:
            chunk = min(65536, pos)
            pos -= chunk
            f.seek(pos)
            buf = f.read(chunk) + buf
        text = buf.decode("utf-8", errors="replace")
    return "\n".join(text.split("\n")[-n:])


@app.get("/api/logs", dependencies=[Depends(require_auth)])
def list_logs():
    logdir = manager.server_dir / "logs"
    if not logdir.is_dir():
        return {"logs": []}
    items = []
    for f in sorted(logdir.iterdir(), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        if not f.is_file():
            continue
        st = f.stat()
        items.append({"name": f.name, "size": st.st_size,
                      "modified": datetime.fromtimestamp(st.st_mtime).isoformat()})
    return {"logs": items}


@app.get("/api/logs/read", dependencies=[Depends(require_auth)])
def read_log(file: str, lines: int = 500):
    if "/" in file or "\\" in file or file.startswith("."):
        raise HTTPException(400, "invalid log name")
    p = manager.server_dir / "logs" / file
    if not p.is_file():
        raise HTTPException(404, "not found")
    lines = max(1, min(lines, 5000))
    return {"file": file, "content": _tail_lines(p, lines)}


if CONFIG["mods_enabled"] and ModIndex is not None:
    # ---------- public mod downloads ----------
    # NOTE: these are intentionally public (no auth) so players who are missing
    # mods can download them without a dashboard login.
    mod_index = ModIndex(CONFIG["server_dir"], BASE)
    MODS_PAGE = (Path(__file__).parent / "mods_page.html").read_text()


    @app.get("/mods", include_in_schema=False)
    def mods_page():
        return HTMLResponse(MODS_PAGE)


    @app.get("/api/mods")
    def api_mods():
        return {"mods": mod_index.list_all()}


    @app.get("/api/mods/check")
    def api_mods_check(ids: str = Query(default="")):
        namespaces = [s.strip() for s in ids.split(",") if s.strip()]
        return mod_index.check(namespaces)


    @app.get("/mods/download/{mod_id}")
    def mods_download(mod_id: str):
        path = mod_index.file_path(mod_id)
        if not path:
            raise HTTPException(404, "mod not hosted here")
        return FileResponse(path, filename=path.name,
                            media_type="application/java-archive")


    @app.get("/api/mods/bundle")
    def api_mods_bundle(ids: str = Query(default="")):
        import io
        namespaces = [s.strip() for s in ids.split(",") if s.strip()]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for ns in namespaces:
                path = mod_index.file_path(ns)
                if path:
                    zf.write(path, path.name)
        buf.seek(0)
        from fastapi.responses import StreamingResponse
        return StreamingResponse(buf, media_type="application/zip",
                                 headers={"Content-Disposition":
                                          "attachment; filename=server-mods.zip"})


    class MappingBody(BaseModel):
        namespace: str
        filename: str


    @app.get("/api/mods/mappings", dependencies=[Depends(require_auth)])
    def api_mappings_get():
        return {"mappings": mod_index.get_mappings()}


    @app.post("/api/mods/mappings", dependencies=[Depends(require_auth)])
    def api_mappings_set(body: MappingBody):
        mod_index.set_mapping(body.namespace.strip(), body.filename.strip())
        return {"ok": True}


    @app.delete("/api/mods/mappings/{namespace}", dependencies=[Depends(require_auth)])
    def api_mappings_delete(namespace: str):
        return {"removed": mod_index.remove_mapping(namespace)}


    @app.post("/api/mods/rescan", dependencies=[Depends(require_auth)])
    def api_mods_rescan():
        return mod_index.refresh(force=True)


# ---------- frontend ----------
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
else:
    @app.get("/")
    def no_frontend():
        return {"error": "frontend/ not found"}


if __name__ == "__main__":
    import uvicorn
    if CONFIG["password"] == "changeme":
        print("WARNING: using default password 'changeme' — set DASHBOARD_PASSWORD")
    uvicorn.run(app, host="0.0.0.0", port=CONFIG["port"])
