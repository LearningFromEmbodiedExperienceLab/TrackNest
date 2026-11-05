# TrackNest

English | [简体中文](README.zh-CN.md)

A self-hosted experiment workspace for individuals and teams, powered by Trackio.

All users share one login page and get their own Panel to create team projects, invite registered users, and view experiments. Administrators generate registration invitation links in User Management and copy them from the invitation result page. Members can leave team projects themselves.

Administrators can edit their own username and other users’ usernames in User Management, and set optional nicknames when inviting users. Users can click their account name in the header to edit their own nickname. Project invitations and member lists show `username (nickname)` when a nickname is set. Before renaming a username, stop that user’s personal instance and owned team projects. Renaming moves their data directory and preserves passwords, tokens, and memberships; update personal experiment upload URLs afterward.

## First-time setup

Requires Linux, Python 3.10+, and Nginx. Run the following commands from `<path-to>/tracknest`, using the same system account throughout.

```bash
cd <path-to>/tracknest
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app app init-db
.venv/bin/flask --app app create-admin admin
```

When prompted, set an administrator password with at least 12 characters. Install the Nginx configuration once:

```bash
sudo cp nginx/manager.conf /etc/nginx/conf.d/trackio-manager.conf
sudo nginx -t && sudo systemctl reload nginx
```

## Start the application

Start in the background. Without storage options, the application uses `instance/` and `data/` inside the project directory:

```bash
./start.sh
```

To choose a persistent storage root, with management state in its `instance/` directory and experiment data in its `data/` directory:

```bash
./start.sh --storage-dir /mnt/trackio-storage
```

Relative paths are resolved against the project directory. Quote paths containing spaces. The selection is saved locally in `.storage.json`, which is excluded from Git. `stop.sh`, `status.sh`, and Flask maintenance commands read that configuration. When restarting a custom deployment, pass the same `--storage-dir` again; omitting it selects the project's default paths. Existing `MANAGER_STATE_DIR` / `MANAGER_DATA_ROOT` environment variables can still override the saved configuration, so keep your environment consistent.

When selecting an empty directory for the first time, the script saves the paths and reports that initialization is required. Run the `init-db` and `create-admin` commands from First-time setup, then start again with the same options. To move an existing deployment, stop it completely and copy `instance/` and `data/` into the chosen root before starting. The script does not move existing files. Run `./start.sh --help` for available options.

Open [http://localhost:9000/login](http://localhost:9000/login). For remote access, replace `localhost` with the server address. After logging in, create a team project in the Panel. From the project page, start its instance, invite members, and get the training integration example and token.

| Purpose | Listen address |
| --- | --- |
| Shared entry point for browsers and training uploads | `9000` |
| Management backend | `127.0.0.1:9001` |
| User and project Trackio instances | `127.0.0.1:9002–9141` |

Only the shared entry point needs external access. Creating users or team projects does not require changes to Nginx.

## Check status and logs

```bash
# Check the management backend, port 9000, and all user/project instances
./status.sh

# Follow management logs; press Ctrl+C to stop following
tail -f instance/gunicorn-error.log
```

`status.sh` exits with code 0 when both HTTP endpoints are healthy and no instance has stopped unexpectedly; otherwise it exits with code 1. Intentionally stopped experiment instances are not considered failures. Access logs are in `instance/gunicorn-access.log`. Each experiment instance stores startup and error logs in `server.log` in its data directory. With a custom `MANAGER_STATE_DIR`, management PID files and logs are also stored there.

## Data storage

The default root is `<path-to>/tracknest`:

| Path | Contents |
| --- | --- |
| `instance/manager.db` | Accounts, invitations, projects, membership permissions, tokens, and instance information |
| `instance/secret.key` | Login session signing key |
| `data/<owner>/projects/<project-id>/` | Project experiment databases, media files, and `server.log` |
| `data/<username>/` | Existing personal instance experiment databases and `server.log` |

Project data is always stored under its creator's directory. Collaborators access the same data. Stopping instances or deleting projects or accounts preserves experiment data; deleting a project does not automatically reconnect its retained data later. After legacy projects are migrated, `data/_shared/` contains only compatibility symlinks for historical file paths.

Before backing up, stop the application completely as described below, then copy both `instance/` and `data/` from their actual storage locations. These directories contain credentials; keep backups secure. Choose a common root with the startup option, or set `MANAGER_STATE_DIR` and `MANAGER_DATA_ROOT` separately. Existing files are not moved automatically.

## Migrate between machines

### Move an entire deployment

1. Stop training uploads, run `./stop.sh` on the old machine, and confirm that management and all experiment instances have stopped.
2. Copy the complete `instance/` and `data/` directories into the new machine's project directory or selected storage root, preserving symlinks. For custom storage, copy the actual directories printed by the scripts and select their destination using `--storage-dir` or the corresponding environment variables. Keep both `manager.db` and `secret.key`.
3. Deploy the same project version on the new machine, recreate `.venv`, install `requirements.txt`, and configure Nginx as described in First-time setup. Do not copy the old virtual environment or recreate the administrator. Make sure the new system account can read and write the copied files.
4. Run `./start.sh` and `./status.sh`. For custom storage, include the same `--storage-dir` option. Startup updates stored data paths, converts internal compatibility links to relative links, and clears stale PID records. Accounts, passwords, team memberships, tokens, and experiment data are preserved. Start experiment instances manually when needed.

Use whole-deployment migration for an empty deployment. Do not overwrite another deployment's existing accounts with the old `manager.db`. You can verify the migration using a backup copy first. Deployment and data directories may differ from the old machine's paths. To update paths manually before startup, run `.venv/bin/flask --app app relocate-data`. Internal instance ports retain their assigned values; ensure those ports are available on the new machine.

### Move one user into an existing deployment

On the source machine, stop the user's personal instance and all team projects they own, then export to a new backup directory:

```bash
.venv/bin/flask --app app export-user alice /tmp/alice-backup
```

Copy the entire `alice-backup/` directory to the target machine, then run this command from the target deployment directory:

```bash
.venv/bin/flask --app app import-user /tmp/alice-backup
```

Only registered users can be exported. The backup includes the password hash, tokens, personal experiments, and team projects created by that user. The imported user keeps their password and own tokens; internal account IDs and ports are reassigned. The imported account is a regular user, and its instances remain stopped. Projects owned by other users are not transferred. Team memberships are not restored by matching usernames; owners must invite members again in the target deployment.

Import refuses to overwrite an existing username, user directory, team project ID, or member token. Copying only `data/alice/` cannot restore accounts and team permissions; retain the exported `user.json` as well. Backups contain login credentials, so restrict access. Export and import reject symlinks pointing outside the user data directory; move any required files into that directory first.

For either migration, update the training client's server address. Unused registration invitation links also need the new host address. External absolute paths and remote file references entered in experiment configurations are outside the local data directory and must be handled separately.

## Stop and restart

Stop management and every user/project experiment instance while preserving data:

```bash
./stop.sh
```

If Gunicorn was previously started manually, press Ctrl+C in its original terminal and wait for it to exit before switching to the scripts. Then run `stop.sh` to clean up experiment instances and use `start.sh` to start management. The scripts do not stop unidentified processes using port 9001. Nginx is managed separately by the system and is not started or stopped by these scripts.

To restart, run `./stop.sh`, followed by `./start.sh`. For custom storage, include the original `--storage-dir` option when starting. Log in to start stopped experiment instances manually. All three scripts can be called from any working directory and print the current management and experiment data directories. Use the same system account and storage environment variables throughout.

Scripts output English by default. Add `--lang zh` for Chinese; this can be combined with `--storage-dir` when starting:

```bash
./start.sh --lang zh
./stop.sh --lang zh
./status.sh --lang zh
```

Terminal colors: cyan for paths and information, green for success or running instances, yellow for stopped instances or warnings, and red for errors or unexpected failures. Colors are automatically disabled when output is redirected. Set `NO_COLOR=1` to disable them explicitly. All three scripts support `--help`.

## Upgrade

Run these steps on the machine hosting TrackNest, using the same system account and storage environment variables as startup. Replace `/path/to/tracknest` with your project directory.

1. Check for local changes. Commit or otherwise save any changes you want to keep before pulling:

   ```bash
   cd /path/to/tracknest
   git status --short
   ```

2. Pause training uploads and stop the application:

   ```bash
   ./stop.sh
   ```

   Confirm shutdown succeeded, then back up the actual state and data directories printed by the script to a location outside the project, preserving symlinks. Include `manager.db`, `secret.key`, and experiment files; also keep `.storage.json` if present.

3. Pull updates and install dependencies. Continue only when each command succeeds; if Git reports local changes or diverging branches, resolve them before proceeding:

   ```bash
   git pull --ff-only
   .venv/bin/pip install -r requirements.txt
   ```

4. Start with your original storage options and check status. For default storage:

   ```bash
   ./start.sh
   ./status.sh
   ```

   For custom storage, use this instead, replacing the path with your existing storage root:

   ```bash
   ./start.sh --storage-dir /mnt/trackio-storage
   ./status.sh
   ```

Startup applies supported management database updates. Keep the existing data and signing key; do not recreate the administrator. Start experiment instances from the Panel when needed. If the release changes Nginx configuration, update the installed configuration and test/reload Nginx as described in First-time setup.

## Uninstall

Replace `/path/to/tracknest` below with your actual project directory.

1. Stop the application using the same system account and storage environment variables as startup:

   ```bash
   cd /path/to/tracknest
   ./stop.sh
   ```

   Before deleting anything, copy the state and data directories printed by the script to a backup outside the project if you want to keep accounts and experiments. Preserve symlinks and include `manager.db` and `secret.key`.

2. If you installed the Nginx configuration from this guide, remove it and reload Nginx:

   ```bash
   sudo rm -- /etc/nginx/conf.d/trackio-manager.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```

3. After confirming shutdown and any required backup, permanently delete the project directory, including its code, `.venv`, and any data stored inside it:

   ```bash
   cd ..
   rm -rf -- /path/to/tracknest
   ```

Storage directories outside the project and external backups remain. Delete them separately only if you also want to remove that data. System Python and Nginx do not need to be uninstalled.
