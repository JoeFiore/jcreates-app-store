/* MC Dashboard frontend */
const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("mc_token") || "";
let curPath = "";
let pollTimer = null;
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function api(method, path, body, formData) {
  const opts = { method, headers: {} };
  if (token) opts.headers["Authorization"] = "Bearer " + token;
  if (formData) { opts.body = formData; }
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { showLogin("Session expired — please log in again."); throw new Error("unauthorized"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}
const get = (p) => api("GET", p);
const post = (p, b) => api("POST", p, b);

/* ---------- login ---------- */
function showLogin(msg) {
  token = ""; localStorage.removeItem("mc_token");
  stopPolling();
  $("loginErr").textContent = msg || "";
  $("login").classList.remove("hidden"); $("app").classList.add("hidden");
  if (ws) { ws.close(); ws = null; }
}
async function doLogin() {
  $("loginErr").textContent = "";
  try {
    const r = await post("/api/login", { password: $("pw").value });
    token = r.token; localStorage.setItem("mc_token", token);
    $("pw").value = "";
    $("login").classList.add("hidden"); $("app").classList.remove("hidden");
    boot();
  } catch (e) { $("loginErr").textContent = "Wrong password."; }
}
$("loginBtn").onclick = doLogin;
$("pw").addEventListener("keydown", (e) => e.key === "Enter" && doLogin());

/* ---------- tabs ---------- */
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  $("tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "files") loadFiles();
  if (b.dataset.tab === "players") loadPlayers();
  if (b.dataset.tab === "backups") loadBackups();
  if (b.dataset.tab === "logs") loadLogs();
  if (b.dataset.tab === "mods") loadMods();
});

/* ---------- server controls ---------- */
let serverManaged = true, serverRcon = false;
function setStatus(s, managed = true, players, rcon = false) {
  serverManaged = managed; serverRcon = rcon;
  const pill = $("statusPill");
  pill.textContent = s + (!managed ? (rcon ? " (via RCON)" : " (external)") : "");
  pill.className = "pill " + s;
  const running = s === "running";
  const limited = !managed && !rcon;  // external with no RCON: look but don't touch
  $("btnStart").disabled = running;
  $("btnStop").disabled = !running || limited;
  $("btnRestart").disabled = !running || limited;
  $("externalNotice").classList.toggle("hidden", managed || !running);
  if (!managed && running) {
    $("noticeText").textContent = rcon
      ? "⚠️ This server was started outside the dashboard — connected via RCON, so commands and stop work, but the live log stream isn't available."
      : "⚠️ This server was already running when the dashboard started — console and stop/restart are unavailable for it.";
  }
  $("cmd").disabled = !running || limited;
  $("sendCmd").disabled = !running || limited;
  if (limited) $("cmd").placeholder = "Console unavailable for external server…";
  else $("cmd").placeholder = "Type a server command…";
}
async function refreshInfo() {
  try {
    const i = await get("/api/server");
    setStatus(i.status, i.managed !== false, i.players, i.rcon === true);
    if (i.uptime_secs != null) {
      const m = Math.floor(i.uptime_secs / 60), h = Math.floor(m / 60);
      $("uptime").textContent = h > 0 ? `${h}h ${m % 60}m up` : `${m}m up`;
    } else $("uptime").textContent = "";
    renderPlayers(i.players);
  } catch {}
}
$("btnStart").onclick = async () => { await post("/api/server/start"); refreshInfo(); };
$("btnStop").onclick = async () => {
  if (confirm("Stop the server? Players will be disconnected.")) {
    await post("/api/server/stop"); refreshInfo();
  }
};
$("btnRestart").onclick = async () => {
  if (confirm("Restart the server?")) { await post("/api/server/restart"); refreshInfo(); }
};
$("btnTakeover").onclick = async () => {
  if (confirm("Take over? This will FORCE-STOP the running server (it may not save cleanly) and restart it under dashboard management.")) {
    const r = await post("/api/server/takeover");
    if (!r.ok) alert(r.error || "Takeover failed");
    refreshInfo();
  }
};

/* ---------- console ---------- */
let ws = null;
function logLine(line, cls) {
  const el = $("log"), div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = line;
  el.appendChild(div);
  while (el.children.length > 2000) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}
function connectWS() {
  if (ws) ws.close();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/console`);
  ws.onopen = () => ws.send(JSON.stringify({ token }));
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "error") { showLogin(); return; }
    if (m.type === "log") logLine(m.line, m.line.startsWith("> ") ? "cmd" : "");
    else if (m.type === "status") setStatus(m.status, m.managed !== false, m.players, m.rcon === true);
    else if (m.type === "players") renderPlayers(m.players);
  };
  ws.onclose = () => setTimeout(() => token && connectWS(), 3000);
}
async function sendCmd() {
  const v = $("cmd").value.trim(); if (!v) return;
  $("cmd").value = "";
  try { await post("/api/console", { command: v }); }
  catch (e) { logLine("!! " + e.message); }
}
$("sendCmd").onclick = sendCmd;
$("cmd").addEventListener("keydown", (e) => e.key === "Enter" && sendCmd());

/* ---------- files ---------- */
function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
async function loadFiles(path = curPath) {
  curPath = path;
  $("crumb").textContent = "/" + path;
  try {
    const d = await get("/api/files?path=" + encodeURIComponent(path));
    const ul = $("filelist"); ul.innerHTML = "";
    for (const it of d.items) {
      const li = document.createElement("li");
      if (it.is_dir) li.className = "dir";
      const icon = it.is_dir ? "📁" : "📄";
      li.innerHTML = `<span class="name">${icon} ${it.name}</span>
        <span class="fsize">${fmtSize(it.size)}</span><span class="rowbtns"></span>`;
      const btns = li.querySelector(".rowbtns");
      if (it.is_dir) {
        li.querySelector(".name").onclick = () =>
          loadFiles(path ? path + "/" + it.name : it.name);
      } else if (it.editable) {
        const eb = document.createElement("button"); eb.textContent = "Edit";
        eb.onclick = () => openEditor(path ? path + "/" + it.name : it.name);
        btns.appendChild(eb);
      }
      const del = document.createElement("button"); del.textContent = "Delete";
      del.onclick = async () => {
        const p = path ? path + "/" + it.name : it.name;
        if (confirm("Delete " + p + "?")) { await post("/api/files/delete", { path: p }); loadFiles(); }
      };
      btns.appendChild(del);
      ul.appendChild(li);
    }
    if (!d.items.length) ul.innerHTML = '<li class="muted">Empty folder</li>';
  } catch (e) { $("filelist").innerHTML = `<li class="err">${e.message}</li>`; }
}
$("upDir").onclick = () => {
  const parts = curPath.split("/").filter(Boolean); parts.pop();
  loadFiles(parts.join("/"));
};
$("newFolder").onclick = async () => {
  const n = prompt("New folder name:"); if (!n) return;
  await post("/api/files/mkdir", { path: curPath ? curPath + "/" + n : n });
  loadFiles();
};
$("upload").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  await api("POST", "/api/files/upload?path=" + encodeURIComponent(curPath), undefined, fd);
  e.target.value = ""; loadFiles();
});

let editingPath = "";
async function openEditor(path) {
  editingPath = path;
  $("edName").textContent = path;
  const d = await get("/api/files/read?path=" + encodeURIComponent(path));
  $("edText").value = d.content;
  $("editor").classList.remove("hidden");
}
$("edClose").onclick = () => $("editor").classList.add("hidden");
$("edSave").onclick = async () => {
  await post("/api/files/write", { path: editingPath, content: $("edText").value });
  $("editor").classList.add("hidden");
};

/* ---------- players ---------- */
function renderPlayers(players) {
  const ul = $("playerlist"); ul.innerHTML = "";
  if (!players || !players.length) { ul.innerHTML = '<li class="muted">No players online</li>'; return; }
  for (const p of players) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="name">🎮 ${p}</span><span class="rowbtns"></span>`;
    const btns = li.querySelector(".rowbtns");
    const kick = document.createElement("button"); kick.textContent = "Kick";
    kick.onclick = () => playerAction("kick", p);
    const ban = document.createElement("button"); ban.textContent = "Ban";
    ban.onclick = () => {
      if (confirm("Ban " + p + "?")) playerAction("ban", p);
    };
    btns.append(kick, ban); ul.appendChild(li);
  }
}
async function playerAction(action, player) {
  const reason = action === "kick" ? prompt("Reason (optional):") || "" : "";
  if (action === "kick" && reason === null) return;
  await post("/api/players/" + action, { player, reason });
  setTimeout(loadPlayers, 800);
}
async function loadPlayers() {
  try { const d = await get("/api/players"); renderPlayers(d.players); } catch {}
}
$("refreshPlayers").onclick = loadPlayers;
$("wlAdd").onclick = async () => {
  const p = $("wlName").value.trim(); if (!p) return;
  await post("/api/players/whitelist-add", { player: p }); $("wlName").value = "";
};
const offlineAction = (action) => async () => {
  const p = $("wlName").value.trim() || prompt("Player name:");
  if (p) await post("/api/players/" + action, { player: p });
};
$("pardonBtn").onclick = offlineAction("pardon");
$("opBtn").onclick = offlineAction("op");
$("deopBtn").onclick = offlineAction("deop");
$("wlRemoveBtn").onclick = offlineAction("whitelist-remove");

/* ---------- backups ---------- */
async function loadBackups() {
  try {
    const d = await get("/api/backups");
    $("worldInfo").textContent = "Worlds: " + (d.world_dirs.join(", ") || "none found");
    const ul = $("backuplist"); ul.innerHTML = "";
    for (const b of d.backups) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="name">💾 ${b.name}</span>
        <span class="fsize">${fmtSize(b.size)}</span><span class="rowbtns"></span>`;
      const btns = li.querySelector(".rowbtns");
      const rs = document.createElement("button"); rs.textContent = "Restore";
      rs.onclick = async () => {
        if (confirm(`Restore ${b.name}? This stops the server and replaces the world.`)) {
          rs.disabled = true; rs.textContent = "Restoring…";
          await post(`/api/backups/${encodeURIComponent(b.name)}/restore`);
          refreshInfo();
        }
      };
      const del = document.createElement("button"); del.textContent = "Delete";
      del.onclick = async () => {
        if (confirm("Delete " + b.name + "?"))
          await post(`/api/backups/${encodeURIComponent(b.name)}/delete`);
        loadBackups();
      };
      btns.append(rs, del); ul.appendChild(li);
    }
    if (!d.backups.length) ul.innerHTML = '<li class="muted">No backups yet</li>';
  } catch (e) { $("backuplist").innerHTML = `<li class="err">${e.message}</li>`; }
}
$("mkBackup").onclick = async (e) => {
  const b = e.target; b.disabled = true; b.textContent = "Backing up…";
  try { await post("/api/backups"); loadBackups(); }
  catch (err) { alert("Backup failed: " + err.message); }
  b.disabled = false; b.textContent = "+ Create backup now";
};

/* ---------- logs ---------- */
async function loadLogs() {
  try {
    const d = await get("/api/logs");
    const sel = $("logFile"); sel.innerHTML = "";
    for (const l of d.logs) {
      const o = document.createElement("option");
      o.value = l.name; o.textContent = `${l.name} (${fmtSize(l.size)})`;
      sel.appendChild(o);
    }
    if (!d.logs.length) {
      $("logview").textContent = "No log files found in <server>/logs/.";
      return;
    }
    readLog();
  } catch (e) { $("logview").textContent = "Error: " + e.message; }
}
async function readLog() {
  const f = $("logFile").value; if (!f) return;
  const n = Math.max(1, Math.min(5000, parseInt($("logLines").value) || 500));
  try {
    const d = await get(`/api/logs/read?file=${encodeURIComponent(f)}&lines=${n}`);
    $("logview").textContent = d.content;
    $("logview").scrollTop = $("logview").scrollHeight;
  } catch (e) { $("logview").textContent = "Error: " + e.message; }
}
$("refreshLogs").onclick = readLog;
$("logFile").addEventListener("change", readLog);

/* ---------- import ---------- */
$("importFile").addEventListener("change", (e) => {
  const f = e.target.files[0];
  $("importName").textContent = f ? f.name : "";
});
$("doImport").onclick = async () => {
  const f = $("importFile").files[0];
  if (!f) { $("importStatus").textContent = "Pick a .zip file first."; return; }
  $("importStatus").textContent = "Importing… (this stops the server first if it is running)";
  $("jarPicker").classList.add("hidden");
  $("acceptEula").classList.add("hidden");
  $("importStart").classList.add("hidden");
  try {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("clean", $("importClean").checked ? "true" : "false");
    const r = await api("POST", "/api/import", undefined, fd);
    let msg = `Imported ${r.files} files. Server jar: ${r.jar}.`;
    $("importStatus").textContent = msg;
    if (r.jars && r.jars.length > 1 && !(r.jars.includes("server.jar") || r.jars.includes("fabric-server-launch.jar"))) {
      const sel = $("jarSelect"); sel.innerHTML = "";
      for (const j of r.jars) { const o = document.createElement("option"); o.textContent = j; sel.appendChild(o); }
      $("jarPicker").classList.remove("hidden");
      $("importStatus").textContent = msg + " Pick the jar that runs the server.";
    }
    if (r.eula_required) {
      $("acceptEula").classList.remove("hidden");
      $("importStatus").textContent += " EULA still needed — accept it below, then start.";
    } else {
      $("importStart").classList.remove("hidden");
    }
  } catch (e) { $("importStatus").textContent = "Import failed: " + e.message; }
};
$("setJar").onclick = async () => {
  try {
    const r = await post("/api/jar", { jar_file: $("jarSelect").value });
    $("importStatus").textContent = "Using jar: " + r.jar;
    $("jarPicker").classList.add("hidden");
    $("importStart").classList.remove("hidden");
  } catch (e) { $("importStatus").textContent = "Could not set jar: " + e.message; }
};
$("acceptEula").onclick = async () => {
  try {
    await post("/api/eula", {});
    $("acceptEula").classList.add("hidden");
    $("importStart").classList.remove("hidden");
    $("importStatus").textContent = "EULA accepted. Ready to start.";
  } catch (e) { $("importStatus").textContent = "EULA failed: " + e.message; }
};
$("importStart").onclick = async () => {
  try {
    await post("/api/server/start"); refreshInfo();
    $("importStatus").textContent = "Server starting — check the Console tab.";
  } catch (e) { $("importStatus").textContent = "Start failed: " + e.message; }
};

/* ---------- boot ---------- */
async function boot() {
  try {
    const cfg = await get("/api/config");
    if (!cfg.mods_enabled) {
      $("modsTabBtn").classList.add("hidden");
      $("tab-mods").classList.add("hidden");
    }
  } catch {}
  refreshInfo();
  connectWS();
  loadFiles("");
  stopPolling();  // avoid stacking intervals on re-login
  pollTimer = setInterval(refreshInfo, 15000);
}
if (token) {
  $("login").classList.add("hidden"); $("app").classList.remove("hidden");
  boot();
}

/* ---------- mods ---------- */
async function loadMods() {
  try {
    const d = await get("/api/mods");
    const mods = d.mods || [];
    $("modsCount").textContent = mods.length + " mod" + (mods.length === 1 ? "" : "s") + " indexed";
    const ul = $("modslist");
    ul.innerHTML = "";
    for (const m of mods) {
      const li = document.createElement("li");
      li.innerHTML = "";
      const name = document.createElement("strong");
      name.textContent = m.name + (m.version ? " " + m.version : "");
      li.appendChild(name);
      const meta = document.createElement("div");
      meta.className = "muted small";
      meta.textContent = "namespace: " + m.id + "  ·  " + m.file + (m.manual ? "  ·  manual" : "");
      li.appendChild(meta);
      ul.appendChild(li);
    }
    const md = await get("/api/mods/mappings");
    const mappings = md.mappings || {};
    const ml = $("mappingslist");
    ml.innerHTML = "";
    for (const [ns, file] of Object.entries(mappings)) {
      const li = document.createElement("li");
      const t = document.createElement("span");
      t.textContent = ns + " → " + file + " ";
      li.appendChild(t);
      const del = document.createElement("button");
      del.textContent = "Remove";
      del.className = "danger";
      del.onclick = async () => {
        await api("DELETE", "/api/mods/mappings/" + encodeURIComponent(ns));
        loadMods();
      };
      li.appendChild(del);
      ml.appendChild(li);
    }
    if (!Object.keys(mappings).length) {
      const li = document.createElement("li");
      li.className = "muted small";
      li.textContent = "No manual mappings.";
      ml.appendChild(li);
    }
  } catch (e) {
    $("modsCount").textContent = "Error: " + e.message;
  }
}
$("rescanMods").onclick = async () => {
  try {
    const r = await post("/api/mods/rescan");
    $("modsCount").textContent = "Rescanned: " + r.scanned + " updated, " + r.total + " total.";
    loadMods();
  } catch (e) { $("modsCount").textContent = "Error: " + e.message; }
};
$("addMapping").onclick = async () => {
  const ns = $("mapNs").value.trim(), file = $("mapFile").value.trim();
  if (!ns || !file) return;
  try {
    await post("/api/mods/mappings", { namespace: ns, filename: file });
    $("mapNs").value = ""; $("mapFile").value = "";
    loadMods();
  } catch (e) { alert("Error: " + e.message); }
};
