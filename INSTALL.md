# Installing Basilisk

Basilisk runs as a container. You need **Docker** or **Podman** — on Windows,
macOS or Linux — and nothing else: the image builds from this repository and
fetches everything it needs (Python packages, the database drivers,
pytincture and the wapyt widgetset) from the internet while it builds.

- **Disk:** about 1.5 GB for the images and build cache.
- **Network during the build:** github.com, pypi.org and deb.debian.org.
- **Time:** the first build takes roughly 3–10 minutes; later ones reuse most of it.

**Windows without Docker?** There is also a native installer, with no
container engine and no HTTPS: see
[section 11](#11-windows-without-docker-native-installer).

Every command below is shown for Docker. **For Podman, replace `docker` with
`podman`** — the compose files, flags and paths are the same. Where Windows
PowerShell differs from macOS/Linux shells, both are given.

**Contents**

1. [Install a container engine](#1-install-a-container-engine)
2. [Get Basilisk](#2-get-basilisk)
3. [Configure](#3-configure)
4. [Start it](#4-start-it)
5. [Connect to a database](#5-connect-to-a-database)
6. [Everyday tasks](#6-everyday-tasks): stop, logs, back up, reset a password, uninstall
7. [Without compose](#7-without-compose)
8. [Reaching Basilisk from other machines](#8-reaching-basilisk-from-other-machines)
9. [Troubleshooting](#9-troubleshooting)
10. [What was actually tested](#10-what-was-actually-tested)
11. [Windows without Docker (native installer)](#11-windows-without-docker-native-installer)
12. [Updating](#12-updating)

---

## 1. Install a container engine

Pick **one** engine. You also need its **compose** tool, which starts
Basilisk from `compose.yaml` with one command.

### Windows 10 (22H2 or later) and Windows 11

**Docker Desktop**

1. Install Docker Desktop from <https://www.docker.com/products/docker-desktop/>,
   or in PowerShell: `winget install Docker.DockerDesktop`.
2. It uses WSL 2. If the installer asks, let it enable WSL, or run
   `wsl --install` in an administrator PowerShell and restart.
3. Start Docker Desktop and wait until it says it is running.
4. Check, in a new PowerShell window:
   ```powershell
   docker version
   docker compose version
   ```

**Podman**

1. Install Podman Desktop from <https://podman-desktop.io/>, or in PowerShell:
   `winget install RedHat.Podman-Desktop` (the command-line only package is
   `RedHat.Podman`).
2. Create and start the Podman machine (a small Linux VM, using WSL 2):
   ```powershell
   podman machine init
   podman machine start
   ```
   Podman Desktop offers to do this on first launch.
3. Install a compose provider. Podman Desktop's **Compose** extension installs
   one; on the command line: `winget install Docker.DockerCompose`.
4. Check:
   ```powershell
   podman version
   podman compose version
   ```

**Git** (to clone): `winget install Git.Git`, or skip it and download the ZIP
in step 2.

### macOS 13 or later (Apple Silicon or Intel)

**Docker Desktop** — install it from <https://www.docker.com/products/docker-desktop/>,
then start it and check `docker version` and `docker compose version`.
Docker-compatible alternatives such as OrbStack or Colima work the same way,
since they provide the `docker` command.

**Podman**

```sh
brew install podman docker-compose      # docker-compose is podman compose's provider
podman machine init
podman machine start
podman compose version
```

Podman Desktop (<https://podman-desktop.io/>) is the graphical alternative.

On Apple Silicon the image builds as native `arm64`; nothing to configure.

### Linux

**Docker Engine** — follow <https://docs.docker.com/engine/install/> for your
distribution and install the Compose plugin (`docker-compose-plugin`). To use
`docker` without `sudo`, add yourself to the `docker` group and log in again:

```sh
sudo usermod -aG docker "$USER"
```

**Podman** (rootless by default; nothing needs `sudo` once it is installed):

```sh
sudo apt install podman                 # Debian, Ubuntu
sudo dnf install podman                 # Fedora, RHEL, CentOS Stream
```

Then a compose provider — either one:

```sh
sudo apt install podman-compose         # or: pipx install podman-compose
sudo dnf install podman-compose
# or Docker's compose plugin, which podman compose also uses
```

Check with `podman compose version`.

SELinux (Fedora, RHEL) needs no extra steps: Basilisk stores its data in a
named volume, not a host folder.

---

## 2. Get Basilisk

```sh
git clone https://github.com/El-Iguana/basilisk_wapyt.git
cd basilisk_wapyt
```

Without Git: on <https://github.com/El-Iguana/basilisk_wapyt> choose
**Code → Download ZIP**, unzip it, and open a terminal in the folder.

> **Windows:** the repository forces Unix line endings (`.gitattributes`), so
> Git's usual CRLF conversion does not touch it. Keep it that way — a `\r` at
> the end of a setting in `.env` becomes part of the value.

---

## 3. Configure

Copy the example settings:

```sh
cp .env.example .env                    # macOS, Linux
```
```powershell
Copy-Item .env.example .env             # Windows PowerShell
```

Open `.env` in any text editor and set, at least:

| Setting | What it does |
|---|---|
| `BASILISK_ADMIN_PASS` | Password of the first account, `admin`. **Used only on the very first start**, when there are no accounts yet. Left empty, it is `change_me`. Either way, Basilisk asks for a new password each time it loads until you change it from the app. |
| `BASILISK_ADMIN_USER` | Its name, if not `admin`. |
| `BASILISK_PORT` | The port on this computer, default `8768`. Change it if 8768 is taken. |
| `POSTGRES_PASSWORD` | Only for the bundled PostgreSQL to try Basilisk with (step 4). Default `change-me-too`. |

> **Windows:** Notepad may save the file as `.env.txt` when file extensions are
> hidden. In File Explorer turn on **View → Show → File name extensions** and
> check that the file is called exactly `.env`.

Everything else in `.env.example` is optional and explained there; the full
list is in [README.md](README.md) and `CLAUDE.md`. Leave
`BASILISK_DATA_DIR` unset: the container keeps its data in `/data`, on its
volume.

---

## 4. Start it

```sh
docker compose up -d --build
```

This builds the image and starts the container in the background. Watch it
come up:

```sh
docker compose ps           # wait for "(healthy)"
docker compose logs -f      # Ctrl+C stops following, not the app
```

Then open **<http://127.0.0.1:8768/basilisk>** (or your `BASILISK_PORT`) and
sign in as `admin` with the password from `.env`. Change it from **Password**
in the toolbar.

> **Use `127.0.0.1`, not `localhost`.** Basilisk (through pytincture) only
> serves signed-in sessions over plain HTTP on a *literal* loopback address,
> and it only listens on this computer. `localhost` is answered with
> *400 Invalid host header*. To use it from other machines, see
> [section 8](#8-reaching-basilisk-from-other-machines).

To also start a **PostgreSQL to try it with**:

```sh
docker compose --profile postgres up -d --build
```

Its connection details are in the next section.

---

## 5. Connect to a database

**New connection** asks which engine (PostgreSQL, MySQL / MariaDB, SQL
Server, CockroachDB, or a SQLite or DuckDB file), then a host, port, user and
password — or, for a file, its name. Because Basilisk runs inside a
container, **"this computer" is not `localhost` from its point of view**.
Which host to type:

| Where the database runs | Host | Notes |
|---|---|---|
| The bundled one (`--profile postgres`) | `postgres`, port `5432` | PostgreSQL, user `postgres`, password `POSTGRES_PASSWORD` from `.env` (default `change-me-too`). |
| Installed on this computer — **Docker Desktop** (Windows, macOS) | `host.docker.internal` | Docker Desktop forwards it to this computer. |
| Installed on this computer — **Podman** (Windows, macOS) | `host.containers.internal` | `host.docker.internal` works too. |
| Installed on this computer — **Linux** | see [below](#a-database-on-the-same-computer-linux) | |
| In another container | that container's name | After connecting it to Basilisk's network: `docker network connect basilisk_default <container>`. |
| Another machine, a server, a cloud database | its host name or IP | TLS options are in the connection editor (PostgreSQL's TLS mode, SQL Server's encryption). |
| Behind an SSH server only | its host and port **as the SSH server sees them** | Turn on **Connect through SSH** in the editor; see [below](#through-an-ssh-server). |

The default ports: PostgreSQL 5432, MySQL / MariaDB 3306, SQL Server 1433,
CockroachDB 26257.

**Check before you guess.** This asks the container itself whether it can
reach a host and port:

```sh
docker compose exec basilisk python manage.py probe host.docker.internal 5432
```

It answers *reachable*, *does not resolve*, or *not reachable* with the reason.
**Test connection** in the editor then checks the credentials as well.

### A database on the same computer, Linux

On Linux, `host.docker.internal` / `host.containers.internal` reach this
computer's **network addresses**, not its `127.0.0.1`. A database listening
only on `127.0.0.1` — the default for most installs, and for test containers
published as `-p 127.0.0.1:5432:5432` — times out or is refused from the
container. Three ways out, pick one:

1. **Host networking** (simplest): start Basilisk with the second compose
   file. Inside it, `127.0.0.1` *is* this computer, so a connection with host
   `127.0.0.1` works. Basilisk still listens on 127.0.0.1 only.
   ```sh
   docker compose down
   docker compose -f compose.host-network.yaml up -d --build
   ```
   Use the same `-f compose.host-network.yaml` on later `ps`, `logs`, `down`
   and `exec` commands. (Linux only: on Windows and macOS, host networking
   means the engine's VM.)
2. **Let the database listen beyond loopback** (PostgreSQL's
   `listen_addresses`, MySQL's `bind-address`, a container published without
   `127.0.0.1:`), then use `host.docker.internal` (Docker) or
   `host.containers.internal` (Podman). Only with a password set and a
   firewall keeping the port off the network.
3. **Run the database in a container** next to Basilisk: the bundled
   `--profile postgres`, or your own container connected to `basilisk_default`.

### SQLite and DuckDB files

Basilisk opens SQLite (`.db`, `.sqlite`, `.sqlite3`, `.db3`) and DuckDB
(`.duckdb`, `.ddb`) files itself, so these connections ask for a **file**
instead of a host. The file is on the machine Basilisk runs on, and only files
inside one folder may be opened, the **database files folder**
(`BASILISK_SQLITE_ROOT`):

| Install | Database files folder |
|---|---|
| compose | `/data/sqlite` in the container, on the `basilisk-data` volume |
| Windows installer | `%LOCALAPPDATA%\Basilisk\databases` |
| `python service.py` | `data/sqlite` in the repository |

A file is typed relative to that folder (`shop.db`, `reports/2026.duckdb`);
`..`, absolute paths and links out of it are refused. **Create the file if it
does not exist** in the editor makes a new, empty one.

To put a file into the container:

```sh
docker cp shop.db basilisk:/data/sqlite/shop.db
```

To use a folder of this computer instead of the volume, uncomment the
`./databases:/data/sqlite:z` line in `compose.yaml` (a `databases` folder next
to it) and run `docker compose up -d` again. `:z` is for SELinux; see
section 6.

> **DuckDB opens a file one way at a time.** Within Basilisk, a DuckDB file is
> open either read-only or read-write: a read-only and a read-write
> connection to the same file cannot both be open. The second one says so;
> **Disconnect** the other first. DuckDB also locks the file against other
> programs while Basilisk has it open.

### Through an SSH server

When a database is only reachable from a server you can SSH into, turn on
**Connect through SSH** in the connection editor: the SSH server's host,
port, user and a password or private key. Host and port above it are then the
database **as the SSH server sees it** (often `127.0.0.1`). Basilisk opens
the tunnel itself; nothing is needed on this computer. **Test** shows the
server's host key fingerprint and **Save** pins it: if the key ever changes,
the connection is refused until you tick *Forget the pinned key*.

From a container, the SSH server's own host follows the table above
(`host.docker.internal` for one on this computer, and so on).

---

## 6. Everyday tasks

All of these run in the `basilisk_wapyt` folder. With host networking, add
`-f compose.host-network.yaml` after `compose`.

**Stop, start, restart, logs**

```sh
docker compose stop
docker compose start
docker compose restart
docker compose logs -f
```

The container restarts with the engine unless you stopped it. (Podman on Linux
does not start containers at boot by itself; `podman generate systemd` or a
Quadlet does, see Podman's documentation.)

**Update to a newer version:** see [section 12](#12-updating).

**Back up the data volume** — the SQLite database of accounts and connections,
`secret.key`, which encrypts the stored database passwords, and the database
files folder. *Without `secret.key` the stored passwords cannot be read.*

**Podman** (any OS) exports and imports volumes itself:

```sh
podman volume export basilisk-data --output basilisk-data.tar
# restore: stop Basilisk, then import into the (existing, empty) volume
podman volume import basilisk-data basilisk-data.tar
```

**Docker** has no export, so a throwaway container copies the volume into a
dedicated `backups` folder:

```sh
# macOS, Linux
mkdir -p backups
docker run --rm -v basilisk-data:/data -v "$PWD/backups:/backup:z" \
  docker.io/library/alpine tar czf /backup/basilisk-data.tgz -C /data .
```
```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force backups | Out-Null
docker run --rm -v basilisk-data:/data -v "${PWD}\backups:/backup" `
  docker.io/library/alpine tar czf /backup/basilisk-data.tgz -C /data .
```

To restore, stop Basilisk, run the same command with
`tar xzf /backup/basilisk-data.tgz -C /data`, and start it again.

> `:z` lets the container write to the folder on Linux with SELinux (Fedora,
> RHEL), where it is otherwise *Permission denied*; elsewhere it does nothing.
> It relabels the folder, so only ever give it a dedicated one like `backups`,
> never your home folder.

**Forgot the admin password**

```sh
docker compose exec -it basilisk python manage.py reset-password admin
```

It asks for the new password twice (`--password-stdin` reads it from standard
input instead). `manage.py users` lists the accounts;
`reset-password NAME --create` makes a new administrator.

**Uninstall**

```sh
docker compose down                 # removes the container, keeps your data
docker compose down -v              # …and deletes the data volume — irreversible
docker image rm localhost/basilisk:latest
```

---

## 7. Without compose

The same thing with plain commands (Podman: replace `docker` with `podman`).

```sh
docker build -f Containerfile -t localhost/basilisk:latest .
docker volume create basilisk-data
docker run -d --name basilisk --restart unless-stopped \
  -p 127.0.0.1:8768:8768 \
  --env-file .env \
  -e BASILISK_CANONICAL_ORIGIN=http://127.0.0.1:8768 \
  --add-host host.docker.internal:host-gateway \
  -v basilisk-data:/data \
  localhost/basilisk:latest
```
```powershell
docker build -f Containerfile -t localhost/basilisk:latest .
docker volume create basilisk-data
docker run -d --name basilisk --restart unless-stopped `
  -p 127.0.0.1:8768:8768 `
  --env-file .env `
  -e BASILISK_CANONICAL_ORIGIN=http://127.0.0.1:8768 `
  --add-host host.docker.internal:host-gateway `
  -v basilisk-data:/data `
  localhost/basilisk:latest
```

- `-f Containerfile` is required with Docker, which otherwise looks for a file
  named `Dockerfile`.
- Leave `BASILISK_BIND` out of `.env` (it is commented out in
  `.env.example`): the image listens on 0.0.0.0 inside the container, which
  the published port needs. Set to 127.0.0.1 there, the page would not load.
- `--add-host …:host-gateway` gives Docker Engine on Linux the
  `host.docker.internal` name; it is harmless elsewhere.
- **Another host port** (say 9000): `-p 127.0.0.1:9000:8768` *and*
  `-e BASILISK_CANONICAL_ORIGIN=http://127.0.0.1:9000`, since the app must know
  the address the browser uses.
- **Host networking on Linux**: replace the `-p` and `--add-host` lines with
  `--network host -e BASILISK_BIND=127.0.0.1`.

---

## 8. Reaching Basilisk from other machines

Basilisk holds credentials for your databases, so it refuses to serve
sign-ins over plain HTTP anywhere but a loopback address. To use it from other
machines, put an HTTPS reverse proxy in front and tell Basilisk its public
address. With [Caddy](https://caddyserver.com/) on the same machine, which
obtains the certificate itself:

1. In `.env`:
   ```
   BASILISK_CANONICAL_ORIGIN=https://sql.example.com
   BASILISK_ALLOWED_HOSTS=sql.example.com
   ```
2. A `Caddyfile`:
   ```
   sql.example.com {
       reverse_proxy 127.0.0.1:8768
   }
   ```
3. `docker compose up -d` (to apply the new settings) and start Caddy.

Keep the `127.0.0.1:` in the published port: only the proxy should reach
Basilisk. `sql.example.com` must resolve to the machine, and ports 80 and 443
must reach Caddy for it to get a certificate (or use your own certificate with
Caddy's `tls` directive). Any HTTPS reverse proxy works the same way —
nginx, Traefik, a cloud load balancer — as long as it sends
`X-Forwarded-Proto: https`, which they do by default.

Mark the connections to important servers as **Production** in the editor:
Basilisk then asks before running anything that is not a read, and shows a
red badge. **Read-only** connections cannot change anything at all.

---

## 9. Troubleshooting

**The page says `Invalid host header`.** You opened `localhost` (or another
name). Use `http://127.0.0.1:8768` exactly, or set up
[section 8](#8-reaching-basilisk-from-other-machines).

**`port is already allocated` / `address already in use`.** Something else
uses 8768. Set `BASILISK_PORT=8778` (any free port) in `.env` and run
`docker compose up -d` again.

**The page does not load at all after a plain `docker run`.** Check that
`.env` does not set `BASILISK_BIND` (an `.env` copied from an older
`.env.example` did): inside the container it must stay 0.0.0.0 (section 7).

**`permission denied … docker.sock` (Linux).** Add yourself to the `docker`
group (section 1) and log in again, or use `sudo docker`.

**`Cannot connect to Podman` / `unable to connect to Podman socket`
(Windows, macOS).** The Podman machine is not running: `podman machine start`.

**`podman compose` says no compose provider was found.** Install
`docker-compose` or `podman-compose` (section 1).

**The build fails downloading something.** The build needs github.com,
pypi.org and deb.debian.org. Behind a proxy, configure it for your engine
(Docker Desktop: *Settings → Resources → Proxies*; Podman: `HTTP_PROXY` /
`HTTPS_PROXY` in the Podman machine or your environment) and build again.

**A connection times out or is refused.** Run the `manage.py probe` command
from [section 5](#5-connect-to-a-database). *Not reachable* on Linux usually
means the database listens on 127.0.0.1 only — see
[A database on the same computer, Linux](#a-database-on-the-same-computer-linux).

**`No such file in the database files folder`.** The file must be inside the
folder of [SQLite and DuckDB files](#sqlite-and-duckdb-files), and its name is
typed relative to it.

**`This DuckDB file is already open in Basilisk the other way`.** A read-only
and a read-write connection to one DuckDB file cannot both be open:
**Disconnect** the other one (navigator menu).

**`docker compose ps` shows `unhealthy`.** `docker compose logs basilisk`
shows why; the last lines usually name the problem.

**Stored passwords stopped working after moving Basilisk.** The data volume's
`secret.key` encrypts them; restoring the database without it loses them. Move
the whole volume (section 6), or re-enter the passwords.

---

## 10. What was actually tested

On Linux x86_64 with rootless Podman 5.8 and Docker Compose v5 as its compose
provider, starting each time from a clean copy of the repository and an empty
volume (2026-10-26):

- `compose.yaml` with the bundled PostgreSQL (`--profile postgres`): the build
  from a clean copy (wapyt fetched from GitHub at the pinned commit), healthy
  within a minute, the first-run password reminder, and a connection to
  `postgres:5432` tested and saved in the browser. `manage.py probe` reached
  the bundled PostgreSQL and could not reach a PostgreSQL on the host's
  127.0.0.1 through `host.containers.internal` — the reason for the Linux part
  of section 5.
- `compose.host-network.yaml` on another port, against the test databases on
  the host's 127.0.0.1: the full browser test suite (Playwright, Chromium) on
  PostgreSQL, SQLite and DuckDB — browsing, editing, the SQL editor, explain
  plans, imports, the ER diagram, schema and data compare.
- `manage.py reset-password` (interactive and `--password-stdin`), a restart
  that keeps the accounts and connections, `podman volume export`, and
  `docker compose down` keeping the volumes.
- The same image as the development container passes the full browser suite
  on all six engines (PostgreSQL, MySQL, SQL Server, SQLite, DuckDB,
  CockroachDB).
- Both compose files validate with Docker Compose (`docker compose config`).

**The Windows installer** is built and tested by the `windows` GitHub Actions
workflow (section 11). Its bundle also builds on Linux, and the launcher's
`--check` (start, sign-in page, stop) passes there.

Not tested here: Docker Engine itself, Docker Desktop, macOS, `arm64`, and
section 8's reverse proxy for Basilisk (the same setup was tested for
Monguana, which shares the plumbing). The steps follow Docker's and Podman's
documented behaviour. If something differs on your system, `manage.py probe`
will show where, and an issue on GitHub is welcome.

---

## 11. Windows without Docker (native installer)

For a computer without Docker: a normal Windows installer that runs Basilisk
directly, for **you, on this computer only**. It needs no administrator rights,
no container engine and no certificate.

**Get it:** `Basilisk-<version>-setup.exe`, attached to each
[release](https://github.com/El-Iguana/basilisk_wapyt/releases) (built by the
`windows` GitHub Actions workflow). Windows 10 or 11, 64-bit; about 150 MB
installed (DuckDB is most of it).

**SmartScreen:** the installer is not code-signed yet, so Windows says
*"Windows protected your PC"*. Choose **More info → Run anyway**.

**What it does:**

- Installs to `%LOCALAPPDATA%\Programs\Basilisk`, with its own Python. It
  does not touch any Python you have.
- Adds **Basilisk**, **Reset Basilisk password** and **Uninstall Basilisk** to
  the Start menu. It can also add a desktop shortcut and start Basilisk when
  you sign in.
- Starting **Basilisk** runs the server in the background and opens
  `http://127.0.0.1:8768/basilisk` in your default browser. If 8768 is taken,
  it uses the next free port. A tray icon offers **Open Basilisk**, **Database
  files folder**, **Show log folder** and **Quit Basilisk**. Starting it again
  while it runs just opens the browser.
- **Plain HTTP is safe here** because it listens on 127.0.0.1 only: nothing
  outside your computer can reach it. Use `127.0.0.1` exactly, not
  `localhost`, which answers *400 Invalid host header*.

**First start:** sign in as **`admin`** with password **`change_me`** (a
message box reminds you). Basilisk then asks you to choose your own password,
and keeps asking each time it loads until you do; **Change password** in that
reminder, or the **Password** button, does it. Forgot it later? Run **Reset
Basilisk password** from the Start menu.

**Databases:** no database server is included. A server on the same computer
is simply `127.0.0.1` with its port: none of the container networking of
section 5 applies. **SQLite and DuckDB files** go in
`%LOCALAPPDATA%\Basilisk\databases` (tray → Database files folder); a
connection names a file inside it.

**Your data** (accounts, saved connections, the key that encrypts their
passwords, the database files folder, logs) is in `%LOCALAPPDATA%\Basilisk`.
Upgrading keeps it: run the new installer over the old one (see
[section 12](#12-updating)). To back it up, quit Basilisk from the tray and
copy that folder. Keep `secret.key` with `basilisk.db`; without it, the saved
passwords cannot be decrypted.

**Uninstall** from Start menu → Uninstall Basilisk, or Settings → Apps. It
asks whether to delete your data too — which includes the **database files
folder** with your SQLite and DuckDB files; the default keeps everything.

**If it does not start**, a message box says so. The reason is in
`%LOCALAPPDATA%\Basilisk\logs\server.log` (tray → Show log folder). The
log from the start before that is `server.previous.log`.

**Command line** (PowerShell), for support or scripts:

```powershell
$b = "$env:LOCALAPPDATA\Programs\Basilisk"
& "$b\python\python.exe" "$b\app\basilisk_launcher.py" --status   # prints the URL if running
& "$b\python\python.exe" "$b\app\basilisk_launcher.py" --stop
& "$b\python\python.exe" "$b\app\basilisk_launcher.py" --check    # start, check the login page, stop
```

**Tested:** every build runs on a GitHub Actions Windows runner, which runs
the unit tests on Windows, installs the build silently, starts it, checks the
login page and the script MIME types, stops it and uninstalls it.

**Not tested yet:** installing by hand on a Windows desktop, and installing a
new version over an earlier one. The checklist for the second is
[docs/UPGRADE_TEST.md](docs/UPGRADE_TEST.md).

---

## 12. Updating

### Knowing there is a new version

Basilisk tells you. The server asks GitHub for the latest release once every
6 hours; when a newer one is out, the toolbar shows an **Update x.y.z** badge
and **About** links to the release. Set `BASILISK_UPDATE_CHECK=off` in `.env`
(or the environment) for a computer that must not call out; About then just
links to the releases page. **About** also shows the version you are running.

### Before you update

Your data is kept by every kind of update below. A backup costs a minute and
lets you go back, though: see *Back up the data volume* in
[section 6](#6-everyday-tasks), or copy `%LOCALAPPDATA%\Basilisk` with
Basilisk quit, on Windows.

### Container, cloned with Git

In the `basilisk_wapyt` folder:

```sh
git pull
docker compose up -d --build
```

Use the same compose file and options you started it with: add
`-f compose.host-network.yaml` after `compose` for host networking, and
`--profile postgres` if you run the bundled PostgreSQL. The data volume
(`basilisk-data`) is kept, and so is the bundled PostgreSQL's data.

If `.env.example` has new settings, copy any you want into your `.env`; a
setting left out keeps its default.

The build leaves the old image behind, untagged; `docker image prune` removes
it.

### Container, downloaded as a ZIP

1. Download the new ZIP (**Code → Download ZIP**, or the source archive on a
   release) and unzip it into a **new** folder.
2. Copy your `.env` from the old folder into the new one.
3. In the new folder, `docker compose up -d --build` (with the same options as
   above).

The compose project, container and volumes have fixed names, so the new
folder takes over the same container and the same data. Delete the old folder
afterwards.

### Windows installer

1. Download the new `Basilisk-<version>-setup.exe` from the
   [releases](https://github.com/El-Iguana/basilisk_wapyt/releases).
2. Run it. **Don't uninstall first.** It recognises the existing install,
   installs into the same folder, and stops a running Basilisk (tray included)
   before replacing anything.
3. Start Basilisk. It may ask again about the desktop and sign-in shortcuts;
   your answers don't remove the ones you already have.

The program files are replaced whole, and `%LOCALAPPDATA%\Basilisk`
(accounts, connections, `secret.key`, the database files, logs) is left
alone. Settings → Apps shows one Basilisk, at the new version.

### From source

```sh
git pull
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode   # if wapyt changed
uv run python service.py
```

### What happens to the database

The new version updates Basilisk's own database the first time it starts, and
only ever *adds* to it (a column, a table). Your data stays as it was. The
databases you manage are never changed by an update.

### Going back to an earlier version

Not supported as such: whether an older version runs on a database a newer
one has updated is not tested. To go back reliably, restore the backup you
made before updating, then install the older version: the release's
`setup.exe` on Windows, or `git checkout vX.Y.Z` and
`docker compose up -d --build` for the container.
