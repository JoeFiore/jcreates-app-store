# JCreates App Store

A personal Umbrel community app store. Right now it holds one app:

## MC Dashboard (`mc-dashboard`, v1.1.0)

A web dashboard for managing a Minecraft Java server — live console, start/stop/
restart, file manager, player moderation, one-click backups, log viewer, RCON —
plus an **Import tab**: upload a zip of a whole server and it extracts and
starts like normal. The server itself runs inside the app container (Java 21),
and all server files live on the app's data volume, so worlds, mods and backups
survive app updates.

**Ports:** dashboard on the Umbrel-assigned app port (the container listens on
8000), **Minecraft on 25565**. This build has **no ModLink / `/mods` hosting**.

### Install it on your Umbrel

1. Push this folder to GitHub as a repo (e.g. `jcreates-app-store`).
   The repo root must keep `umbrel-app-store.yml`, and the app must stay in
   the `mc-dashboard/` folder.
2. On your Umbrel: open the **App Store**, click the **three dots** in the
   top-right, choose **Community App Stores**, paste the repo URL, and add it.
3. Open the new **JCreates** store, install **MC Dashboard**. First install
   builds the Docker image on the box, which takes a few minutes — that's normal.
4. **First login:** the dashboard password is generated on first install and
   saved in the app's data dir. Read it from the Umbrel terminal:
   `cat ~/umbrel/app-data/mc-dashboard/dashboard-password.txt`
   (If that file is missing, the fallback password is `changeme`.)
5. **Add a server:** open the app, go to the **Import** tab, upload your
   server zip, accept the EULA if it asks, pick the server jar if it asks,
   then press Start. Players join on `umbrel.local:25565` at home, or
   `<Tailscale address>:25565` away from home.
6. **Updating later:** push a new version to the same repo, bump `version`
   in `mc-dashboard/umbrel-app.yml`, and Umbrel will offer the update.
   Server files are on the app data volume, so they survive updates.

### Tweaks

- **More RAM for the server:** edit the `JAVA_ARGS` line in the app's
  `docker-compose.yml` and restart the app from the Umbrel dashboard.
- **Newer/older Java:** change the `openjdk-21-jre-headless` package in the
  `Dockerfile` and reinstall the app.

### Layout

```
jcreates-app-store/
├── umbrel-app-store.yml   # the store manifest
├── README.md
└── mc-dashboard/
    ├── umbrel-app.yml     # app manifest (id, version, port, ...)
    ├── docker-compose.yml # one container: dashboard + Java server runtime
    ├── Dockerfile         # Python 3.12 + Java 21 + dashboard source
    ├── export.sh          # generates the dashboard login password once
    ├── icon.svg
    └── app/               # dashboard source (backend + frontend)
        ├── entrypoint.sh  # serves backend.docker_app (no /mods routes)
        ├── backend/
        └── frontend/
```
