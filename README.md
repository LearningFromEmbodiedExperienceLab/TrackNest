# TrackNest

English | [简体中文](README.zh-CN.md)

A self-hosted experiment workspace for individuals and teams, powered by Trackio.

All users share one login page and get their own Panel to create team projects, invite registered users, and view experiments. Administrators generate registration invitation links in User Management and copy them from the invitation result page. Members can leave team projects themselves.

Administrators can edit their own username and other users’ usernames in User Management, and set optional nicknames when inviting users. Users can click their account name in the header to edit their own nickname. Project invitations and member lists show `username (nickname)` when a nickname is set. Before renaming a username, stop that user’s personal instance and owned team projects. Renaming moves their data directory and preserves passwords, tokens, and memberships; update personal experiment upload URLs afterward.

## Reuse the UI design

[`tracknest-ui/`](tracknest-ui/) is the self-contained design package. `VERSION` records the current `1.0.0` release, `CHANGELOG.md` records changes, `assets/` holds components, layouts, light/dark themes, the theme script, icons and standalone HTML examples, and `references/` contains the contract and visual reference. TrackNest serves these assets directly at `/static/ui/`; business pages remain in `templates/`.

Copy the entire `tracknest-ui/` folder into your personal skills directory to invoke `$tracknest-ui` in another project. The skill guides copying the assets and version file before adding that project's content and interactions; consuming projects do not upgrade automatically. See [`SKILL.md`](tracknest-ui/SKILL.md). To preview, run `python -m http.server 9188 --bind 127.0.0.1` inside `assets/` and open `example.html` for components or `workspace.html` for the layout.

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

## Log training through an SSH tunnel

Keep the tunnel running on the training machine, for example `ssh -N -L 9000:localhost:9000 PCL-4090-syw`. Open [http://localhost:9000/login](http://localhost:9000/login), sign in to get the personal or team URL and token. Personal spaces support multiple projects: changing `trackio.init(project=...)` does not change the upload URL or token.

Keep the SSH tunnel connected, download the client wheel from the server into any directory, then install and configure it once with your training Python. Replace the host for direct server access. For team spaces, use the `/p/<project-id>/` URL shown on the project page:

```bash
curl --noproxy '*' -fLO http://localhost:9000/client/tracknest_client-0.1.6-py3-none-any.whl
python -m pip install --upgrade ./tracknest_client-0.1.6-py3-none-any.whl
tracknest login --server-url http://localhost:9000/u/<username>/
```

Enter the token from the page when prompted. Settings are saved in `~/.config/tracknest/client.json` (respecting `XDG_CONFIG_HOME`) with mode `0600`. Run login again after changing spaces or rotating the token. No per-terminal URL, token, or NO_PROXY setup is needed.

Each account has one client token for its personal space and every team project. Membership determines read/write access to each team project. Rotating the account token invalidates its old credentials across all spaces. Older project-specific tokens are no longer used for authentication; existing team clients should log in again with the account token shown on the page.

`login` verifies credentials with the manager and succeeds even when the experiment instance is stopped. It never starts the instance. `tracknest.init()` automatically starts a stopped instance and waits until it is ready before initializing training logs. Use `tracknest start` before querying experiment data; `status` reports authentication and instance state separately. `finish()` does not stop the instance.

Change the training import to `import tracknest as trackio`; keep the existing `init/log/finish` calls. Run `python test.py` from any directory afterward. Only the configured TrackNest connection uses `trust_env=False`; HTTP/SOCKS proxy environment variables and other training/download requests remain unchanged.

Status logs are in English with a blue `tracknest:` prefix: cyan for connection/progress, green for success, yellow for pending-data warnings, and red for upload failures. Redirected output and `NO_COLOR` disable colors. Logs report connection verification, acknowledged metric counts, upload waiting, and the final synchronization result. Initialization fails immediately if the server or token is invalid. If connectivity fails during training, Trackio keeps its local buffer and the client reports incomplete synchronization; initializing the same project retries buffered data. Counts cover acknowledged metrics in the current process, including replayed buffers.

Native `import trackio` retains its own behavior: it does not read this configuration and writes locally without a remote URL. Unset `TRACKIO_SPACE_ID` when using TrackNest. The manager bypasses environment proxies for instance startup checks and forwarded requests. Stopped instances return 503; invalid credentials or permissions return 401/403.

## Client status and instance commands

Use these commands from your training environment, without entering the repository:

```bash
tracknest status                  # Configuration, authentication, instance state, pending local cache
tracknest start                   # Start the configured instance and wait until ready
tracknest stop                    # Stop the configured instance; affects all its training uploads
tracknest projects                # Remote projects in the configured space
tracknest runs my-project-test    # Run names, IDs, and creation times
tracknest status --lang zh        # Chinese output (English by default)
tracknest --version               # Installed client version
tracknest --help
```

The same commands work with `python -m tracknest`, for example `python -m tracknest status`, to select a specific Python environment. Queries are read-only, preserve training proxies, and never display the token. `status` checks the cache for the configured server even while offline; counts exclude in-memory upload queues in active training processes. Missing configuration, connection/query failures, stopped instances in `status`, or unreadable cache databases return exit code 1. Successful queries return 0, with a warning if pending cache exists. `start` and `stop` manage the instance selected by the saved login URL; repeated requests are safe. Personal instances can be managed with their account token. Team owners and editors can start instances (including automatically during training); only owners can stop them, and viewers can only query state. Stop training uploads before running `stop`, since it affects every run using that instance. Instance management does not replay cached data; initializing the same project retries pending uploads. Update both the server and client to use these features.

Terminal output is fully colored: verified credentials and running instances are green; stopped instances and unavailable authentication are red. Headings, labels, paths, tables, and hints also use colors. Redirected output and `NO_COLOR` remain plain text. Each command leaves one blank line before and after its output.

CLI output defaults to English. Add `--lang zh` before or after any subcommand, such as `tracknest --lang zh projects` or `tracknest start --lang zh`. Status groups connection information and presents pending uploads in an aligned table with totals; projects and runs also use tables. Stopped instances produce a short `tracknest start` instruction before the SDK is contacted. HTTP and connection failures use concise messages instead of Space diagnostics and HTTP documentation links.

Use `tracknest --version` to check the client installed in your training environment. If it still says to start instances from the web panel, upgrade to `0.1.6` using the wheel commands above with the same Python used for training. The server must be updated to provide the new wheel and instance-management API.

## Check status and logs

```bash
# Check the management backend, port 9000, and all user/project instances
./status.sh

# Follow management logs; press Ctrl+C to stop following
tail -f instance/gunicorn-error.log
```

`status.sh` exits with code 0 when both HTTP endpoints are healthy and no instance has stopped unexpectedly; otherwise it exits with code 1. Intentionally stopped experiment instances are not considered failures. Access logs are in `instance/gunicorn-access.log`. Each experiment instance stores startup and error logs in `server.log` in its data directory. With a custom `MANAGER_STATE_DIR`, management PID files and logs are also stored there.

## Invite collaborators to a personal project

In the Panel, open **Manage experiment projects** under Personal experiment space, expand a project, select a registered collaborator and a viewer or editor role, then choose **Invite and convert to team project**. Pause training and stop the personal instance first. The project must satisfy existing team naming rules and must not duplicate a team project you already own.

Only the selected project is converted. Existing runs, metrics, media, artifacts, and traces are preserved; other personal projects remain private. The project appears under Team projects, where its owner can add/remove members or change their roles. Start the team instance so collaborators can use its URL with their own account tokens.

The owner's original personal upload URL, project name, and account token continue to work. Restart the personal instance to continue using that URL; new uploads and the team instance use the same storage. Conversion keeps relative compatibility links inside the user's data directory. Preserve these links when backing up or relocating user data.

## Delete a personal experiment project

In the Panel, choose **Manage experiment projects** under Personal experiment space. Pause training uploads and stop your personal instance, expand the project, enter its exact name, then confirm **Permanently delete project data**. This removes its runs, metrics, media, artifacts and local trace data. Other personal projects and team projects are unaffected. Logging to the same name later creates the project again.

This differs from deleting a team project, which revokes memberships but retains experiment files.

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
4. Run `./start.sh` and `./status.sh`. For custom storage, include the same `--storage-dir` option. Startup updates stored data paths, converts internal compatibility links to relative links, and clears stale PID records. Accounts, passwords, team memberships, tokens, and experiment data are preserved. Start experiment instances from the Panel, with `tracknest start`, or automatically when training initializes.

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

To restart, run `./stop.sh`, followed by `./start.sh`. For custom storage, include the original `--storage-dir` option when starting. Stopped experiment instances can be started from the Panel, with `tracknest start`, or automatically when training initializes. All three scripts can be called from any working directory and print the current management and experiment data directories. Use the same system account and storage environment variables throughout.

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
