"""Manages the Minecraft server subprocess: start/stop/restart, console I/O,
player tracking, and broadcasting log lines to websocket clients.

If a server is already running when the dashboard starts (dashboard restarted,
or server launched outside the dashboard), it is detected via a PID file and
a process scan, and shown as running but unmanaged — the dashboard can't
reattach to another process's console pipes, so console/restart are limited
until you Take over (restarts it under dashboard management)."""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import psutil

from .rcon import Rcon, RconError

JOIN_RE = re.compile(r"\]: (\w+) joined the game")
LEFT_RE = re.compile(r"\]: (\w+) left the game")
LIST_RE = re.compile(r"There are \d+ of a max of \d+ players online:(.*)")


class ServerManager:
    def __init__(self, server_dir: str | Path, jar_file: str,
                 java_args: str = "-Xmx2G -Xms1G",
                 rcon_host: str = "127.0.0.1", rcon_port: int = 25575,
                 rcon_password: str = ""):
        self.server_dir = Path(server_dir).resolve()
        self.jar_file = jar_file
        self.java_args = java_args
        self.rcon_host = rcon_host
        self.rcon_port = rcon_port
        self.rcon_password = rcon_password
        self.proc: subprocess.Popen | None = None
        self.status = "stopped"          # stopped | starting | running | stopping
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.players: set[str] = set()
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self.pid_file = self.server_dir / ".mc-dashboard.pid"
        self.external_pid: int | None = None  # running, but not our child
        self.detect_running()

    # ---------- already-running detection ----------
    def _clear_pid_file(self) -> None:
        try:
            self.pid_file.unlink()
        except FileNotFoundError:
            pass

    def _is_our_server(self, pid: int) -> bool:
        """True if pid looks like our configured server jar (guards PID reuse)."""
        if pid == os.getpid():
            return False
        try:
            cmdline = " ".join(psutil.Process(pid).cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        return self.jar_file in cmdline

    def _adopt(self, pid: int) -> None:
        self.external_pid = pid
        try:
            self.started_at = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            self.external_pid = None
            return
        self._set_status("running")
        self._broadcast({"type": "log", "line":
                         f"--- detected already-running server (pid {pid}); "
                         "console unavailable until Take over ---"})

    def detect_running(self) -> None:
        """Adopt an already-running server, if any."""
        # Shells that may wrap the real server process (start scripts etc.)
        # must not be adopted — killing/adopting them leaves the child behind.
        shells = {"bash", "sh", "dash", "zsh", "fish",
                  "cmd.exe", "cmd", "powershell.exe", "powershell",
                  "pwsh.exe", "pwsh"}
        pid = None
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
            except (ValueError, OSError):
                pid = None
        if pid is not None and self._is_our_server(pid):
            self._adopt(pid)  # started by a previous dashboard run
            return
        self._clear_pid_file()  # stale pid file
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                info = proc.info
                cmdline = " ".join(info["cmdline"] or [])
                name = (info["name"] or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if info["pid"] == os.getpid() or name in shells:
                continue
            if self.jar_file in cmdline:
                self._adopt(info["pid"])  # started outside the dashboard
                return

    @property
    def managed(self) -> bool:
        return self.external_pid is None

    @property
    def rcon_configured(self) -> bool:
        return bool(self.rcon_password)

    # ---------- RCON ----------
    def rcon_command(self, cmd: str) -> dict:
        """Run a command via RCON. Works even for externally-started servers."""
        if not self.rcon_configured:
            return {"ok": False, "error": "RCON not configured in dashboard"}
        try:
            output = Rcon(self.rcon_host, self.rcon_port,
                          self.rcon_password).command(cmd)
        except RconError as e:
            return {"ok": False, "error": f"RCON error: {e}"}
        return {"ok": True, "output": output}

    # ---------- pub/sub for log lines ----------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(msg)

    def _set_status(self, status: str) -> None:
        self.status = status
        self._broadcast({"type": "status", "status": status,
                         "managed": self.managed,
                         "rcon": self.rcon_configured,
                         "players": sorted(self.players)})

    # ---------- log parsing ----------
    def _handle_line(self, line: str) -> None:
        self._broadcast({"type": "log", "line": line})
        m = JOIN_RE.search(line)
        if m:
            self.players.add(m.group(1))
            self._broadcast({"type": "players", "players": sorted(self.players)})
            return
        m = LEFT_RE.search(line)
        if m:
            self.players.discard(m.group(1))
            self._broadcast({"type": "players", "players": sorted(self.players)})
            return
        m = LIST_RE.search(line)
        if m:
            names = [n.strip() for n in m.group(1).split(",") if n.strip()]
            self.players = set(names)
            self._broadcast({"type": "players", "players": sorted(self.players)})

    def _pump_output(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                self._handle_line(line)
        finally:
            # process ended
            code = self.proc.wait()
            self.exit_code = code
            self.proc = None
            self.players = set()
            self._clear_pid_file()
            self._set_status("stopped")
            self._broadcast({"type": "log",
                             "line": f"--- server process exited (code {code}) ---"})

    # ---------- lifecycle ----------
    def start(self) -> dict:
        if self.proc is not None and self.proc.poll() is None:
            return {"ok": False, "error": "server already running"}
        if self.external_pid is not None:
            return {"ok": False,
                    "error": "server is already running outside the dashboard — "
                             "use Take over to bring it under dashboard management"}
        jar = self.server_dir / self.jar_file
        if not jar.exists():
            return {"ok": False, "error": f"jar not found: {jar}"}
        cmd = ["java", *self.java_args.split(), "-jar", str(jar), "nogui"]
        self._set_status("starting")
        self._broadcast({"type": "log",
                         "line": "--- starting: " + " ".join(cmd) + " ---"})
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(self.server_dir),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=1,
            )
        except FileNotFoundError:
            self._set_status("stopped")
            return {"ok": False, "error": "java not found on PATH"}
        self.started_at = time.time()
        self.exit_code = None
        self.external_pid = None
        try:
            self.pid_file.write_text(str(self.proc.pid))
        except OSError:
            pass
        self._reader = threading.Thread(target=self._pump_output, daemon=True)
        self._reader.start()
        self._set_status("running")
        # ask the server who is online, in case players were already there
        self.send_command("list")
        return {"ok": True, "status": self.status}

    def stop(self, timeout: int = 15) -> dict:
        if self.external_pid is not None:
            if not self.rcon_configured:
                return {"ok": False,
                        "error": "server is running outside the dashboard — "
                                 "use Take over first (it will be force-stopped), "
                                 "or configure RCON for graceful stop"}
            res = self.rcon_command("stop")
            if not res["ok"]:
                return res
            try:
                psutil.Process(self.external_pid).wait(timeout=timeout)
            except psutil.TimeoutExpired:
                return {"ok": False,
                        "error": "server did not stop in time — use Take over"}
            except psutil.NoSuchProcess:
                pass
            self.external_pid = None
            self.started_at = None
            self.players = set()
            self._clear_pid_file()
            self._set_status("stopped")
            return {"ok": True, "status": self.status}
        if self.proc is None or self.proc.poll() is not None:
            return {"ok": False, "error": "server not running"}
        self._set_status("stopping")
        self.send_command("stop")
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self._broadcast({"type": "log",
                             "line": "--- server did not stop in time, killed ---"})
        return {"ok": True, "status": self.status}

    def restart(self) -> dict:
        r = self.stop()
        if not r["ok"]:
            return r
        # wait for the pump thread to notice the exit
        for _ in range(100):
            if self.proc is None:
                break
            time.sleep(0.1)
        return self.start()

    def kill(self) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            return {"ok": False, "error": "server not running"}
        self.proc.kill()
        return {"ok": True}

    def takeover(self) -> dict:
        """Force-stop an externally-running server and start it managed."""
        if self.external_pid is None:
            return {"ok": False, "error": "no external server to take over"}
        pid = self.external_pid
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=20)
            except psutil.TimeoutExpired:
                p.kill()
        except psutil.NoSuchProcess:
            pass
        self.external_pid = None
        self.started_at = None
        self.players = set()
        self._clear_pid_file()
        self._set_status("stopped")
        self._broadcast({"type": "log", "line":
                         f"--- took over: stopped external server (pid {pid}) ---"})
        return self.start()

    # ---------- console ----------
    def send_command(self, cmd: str, quiet: bool = False) -> dict:
        """Send a command. quiet=True skips the console echo (for internal
        refreshes like the player list) while still executing the command."""
        if self.external_pid is not None:
            if not self.rcon_configured:
                return {"ok": False,
                        "error": "server is running outside the dashboard — "
                                 "console unavailable until Take over, "
                                 "or configure RCON"}
            res = self.rcon_command(cmd)
            if not quiet:
                self._broadcast({"type": "log", "line": f"> {cmd.strip()}"})
            if res["ok"]:
                if not quiet:
                    for line in res["output"].splitlines() or ["(no output)"]:
                        self._broadcast({"type": "log", "line": line})
                return {"ok": True}
            return res
        if self.proc is None or self.proc.poll() is not None:
            return {"ok": False, "error": "server not running"}
        if self.proc.stdin is None:
            return {"ok": False, "error": "no stdin available"}
        try:
            self.proc.stdin.write((cmd.strip() + "\n").encode())
            self.proc.stdin.flush()
        except BrokenPipeError:
            return {"ok": False, "error": "server stdin closed"}
        if not quiet:
            self._broadcast({"type": "log", "line": f"> {cmd.strip()}"})
        return {"ok": True}

    def refresh_players(self) -> dict:
        """Refresh the player list. Synchronous via RCON for external servers;
        fire-and-forget (parsed by the log pump) for managed ones."""
        if self.external_pid is not None:
            if not self.rcon_configured:
                return {"ok": False,
                        "error": "server is external and RCON is not configured"}
            res = self.rcon_command("list")
            if not res["ok"]:
                return res
            m = LIST_RE.search(res["output"])
            names = ([n.strip() for n in m.group(1).split(",") if n.strip()]
                     if m else [])
            self.players = set(names)
            self._broadcast({"type": "players", "players": sorted(names)})
            return {"ok": True, "players": sorted(names)}
        if self.proc is None or self.proc.poll() is not None:
            return {"ok": False, "error": "server not running"}
        return self.send_command("list", quiet=True)

    def info(self) -> dict:
        uptime = None
        if self.started_at and self.status in ("running", "starting"):
            uptime = int(time.time() - self.started_at)
        return {"status": self.status, "players": sorted(self.players),
                "uptime_secs": uptime, "exit_code": self.exit_code,
                "managed": self.managed, "rcon": self.rcon_configured,
                "server_dir": str(self.server_dir), "jar": self.jar_file}
