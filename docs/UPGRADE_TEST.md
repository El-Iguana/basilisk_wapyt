# Upgrade test checklist

A hand test that an existing install updates in place and keeps its data,
following [INSTALL.md §12](../INSTALL.md#12-updating). The GitHub Actions
Windows job only tests a fresh install, so updating over an earlier version
needs this. Run it before a release that changes the installer, the launcher
or the database, and at least once for the Windows installer (never done yet:
the first installer is the first release, so this starts with the second).

Record each run at the bottom: date, from → to, machine, result.

## Windows installer

**Machine:** Windows 10 22H2 or 11, 64-bit; a VM snapshot is ideal. You need
the **old** and the **new** `Basilisk-<version>-setup.exe` from the
[releases](https://github.com/El-Iguana/basilisk_wapyt/releases), and a
database server the VM can reach (or only SQLite and DuckDB files, which need
nothing else).

### 1. Set up the old version

- [ ] Install the old version. Tick **both** shortcut options (desktop, and
      start when I sign in).
- [ ] Sign in as `admin` / `change_me`, and change the password when asked.
- [ ] Create a **second account** under Users.
- [ ] Add a **connection with a password** (PostgreSQL, MySQL or SQL Server),
      test it, save it. The password is what proves `secret.key` survived.
- [ ] Put a SQLite file and a DuckDB file in the **databases folder** (tray →
      Database files folder), add a connection to each, and edit a row.
- [ ] Open a table, **resize and reorder** a column, freeze one. This is
      per-user UI state in the database.
- [ ] Type something in a SQL editor and run it, so the draft and the history
      are saved.
- [ ] Note what's in `%LOCALAPPDATA%\Basilisk` (files and sizes) and the
      version in Settings → Apps.
- [ ] Leave Basilisk **running** (tray icon visible), with a browser tab open.

### 2. Update

- [ ] Run the new installer. **Don't** uninstall or quit Basilisk first.
- [ ] It offers the same folder (`%LOCALAPPDATA%\Programs\Basilisk`) or doesn't
      ask, and finishes without a "files in use" or "close applications" error.
- [ ] The old tray icon is gone (the installer stopped the server).
- [ ] Let it start Basilisk at the end ("Start Basilisk now").

### 3. Check the new version

- [ ] The browser opens and the login page loads. Reload the old tab too: it
      should show the login page or the app, not a broken page.
- [ ] Sign in with the **changed** password. There is no `change_me` prompt.
- [ ] **About** shows the new version, and the release check
      says "This is the latest release" (or "Could not check" offline, which is
      fine).
- [ ] The **second account** still exists and can sign in.
- [ ] The **password connection** connects without re-entering the password.
- [ ] The **SQLite and DuckDB** connections still open, with the edited rows.
- [ ] The table keeps its **column widths, order and frozen column**.
- [ ] The SQL editor's **draft and history** are still there.
- [ ] Settings → Apps lists **one** Basilisk, at the new version.
- [ ] The Start menu, desktop and sign-in shortcuts all start the new version.
      Sign out and back in for the sign-in one; it starts in the tray only.
- [ ] `%LOCALAPPDATA%\Basilisk\logs\server.log` has no traceback.
- [ ] `%LOCALAPPDATA%\Basilisk` still has `basilisk.db`, `secret.key` and the
      `databases` folder with its files.

### 4. Uninstall and reinstall (keeps data)

- [ ] Uninstall, and answer **No** to "Also delete your Basilisk data?".
- [ ] `%LOCALAPPDATA%\Programs\Basilisk` is gone; `%LOCALAPPDATA%\Basilisk`
      is still there.
- [ ] Install the new version again: everything from step 3 is still there.

## Container (compose)

Linux with Podman or Docker; quicker, and worth a run when the image or the
database changes.

- [ ] `git checkout v<old>` in a clean clone; `.env` from `.env.example`;
      `docker compose up -d --build`.
- [ ] As in Windows step 1: change the admin password, add a second account, a
      password connection and a column layout.
- [ ] `git checkout v<new>` (or `git pull` on `main`), then
      `docker compose up -d --build`.
- [ ] As in Windows step 3: sign in, About version, accounts, the connection
      without re-entering its password, the layout.
- [ ] `docker compose logs basilisk` has no traceback.
- [ ] Repeat with `compose.host-network.yaml` if that's what you use.

## Runs

| Date | From → to | Machine | Result |
|---|---|---|---|
| | | | |
