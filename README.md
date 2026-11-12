# TrackNest

English | [简体中文](README.zh-CN.md)

A self-hosted experiment workspace powered by Trackio, with personal and team projects, member permissions, and training logs. Administrators invite new users through registration links; users manage experiments from their Panel.

## Deployment

Requires Linux, Python 3.10+, and Nginx. Run these commands in the project directory using the same system account throughout.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app app init-db
.venv/bin/flask --app app create-admin admin
sudo cp nginx/manager.conf /etc/nginx/conf.d/trackio-manager.conf
sudo nginx -t && sudo systemctl reload nginx
./start.sh
./status.sh
```

Set an administrator password of at least 12 characters. Open [http://localhost:9000/login](http://localhost:9000/login), replacing the host for remote access. Only expose port `9000`; management port `9001` and experiment ports `9002–9141` listen on `127.0.0.1`.

```bash
./stop.sh                                      # Stop management and all experiments; retain data
./start.sh --storage-dir /mnt/trackio-storage   # Optional custom storage root
./status.sh --lang zh                          # Scripts default to English; support Chinese and --help
```

Storage defaults to `instance/` and `data/` in the project directory. Custom paths are recorded in `.storage.json`; pass the same `--storage-dir` on every restart or startup returns to the default paths. After selecting an empty directory, run `init-db` and `create-admin` above, then start with the same options. Moving existing storage requires stopping and copying data yourself. `MANAGER_STATE_DIR` and `MANAGER_DATA_ROOT` override saved configuration.

## Training integration

Connect from the training machine. If SSH forwarding is needed, keep this tunnel running:

```bash
ssh -N -L 9000:localhost:9000 <server>
```

Sign in through the browser and copy your personal or team URL and account token. Install the client with your training Python:

```bash
curl --noproxy '*' -fLO http://localhost:9000/client/tracknest_client-0.1.8-py3-none-any.whl
python -m pip install --no-deps --upgrade ./tracknest_client-0.1.8-py3-none-any.whl
tracknest login --server-url http://localhost:9000/u/<username>/
```

Replace the host for direct access; team spaces use `/p/<project-id>/`. Enter the token when prompted. Settings are saved to `~/.config/tracknest/client.json` (supports `XDG_CONFIG_HOME`, mode `0600`). Log in again after switching spaces or rotating your token. One account token covers personal and team spaces; membership determines permissions.

**The client has no mandatory dependencies and does not upgrade existing packages.** Training and operational CLI commands require a working Trackio SDK in the same environment (tested with `0.45.0`); missing dependencies are reported explicitly. Importing, `--help`, and `--version` work without the SDK. Conflicts caused by earlier installations require separate repair.

```python
import tracknest as trackio

trackio.init(project="my-project", name="baseline")
for step in range(25):
    trackio.log({"loss": 1 / (step + 1)}, step=step)
trackio.finish()
```

`init()` starts the instance automatically; `finish()` waits for uploads and keeps it running. Network failures leave data buffered locally; initializing the same project retries it. Only TrackNest requests bypass proxies; training proxy variables stay unchanged. Unset `TRACKIO_SPACE_ID` before use. Native `import trackio` does not read the client configuration.

```bash
tracknest status                  # Connection, instance state, and pending local cache
tracknest start                   # Start the configured instance
tracknest stop                    # Stop the instance; affects all its training uploads
tracknest projects                # Remote projects
tracknest runs my-project        # Runs in a project
tracknest status --project <team-project-name-or-id>
tracknest status --lang zh        # English by default
tracknest --version
tracknest --help
```

Use `python -m tracknest` to select a specific Python environment. `status/start/stop` accept `--project` without changing the saved training connection; use IDs for duplicate team names. Team owners and editors can start instances; only owners can stop them. Pause training uploads before stopping.

## Project management

- Create team projects in the Panel, invite registered users, and assign viewer or editor permissions.
- Share a personal project: pause training, stop the personal instance, then choose **Invite and convert to team project** under **Manage experiment projects**. Data is retained, and the owner's original personal URL and project name still work.
- Permanently delete a personal project: stop its instance, then confirm its full name under **Manage experiment projects**. Deleting a team project or account retains experiment files.
- Administrators can rename usernames after stopping the user's personal instance and owned team instances. Update personal upload URLs afterward. Users can edit their own nicknames.

## Data and maintenance

| Path | Contents |
| --- | --- |
| `instance/manager.db` | Accounts, permissions, tokens, and instance information |
| `instance/secret.key` | Session signing key |
| `instance/gunicorn-*.log` | Management logs |
| `data/<username>/` | Personal experiments and instance logs |
| `data/<owner>/projects/<project-id>/` | Team experiments and instance logs |

Instance logs are named `server.log`. Before backing up, pause training and run `./stop.sh`. Copy both `instance/` and `data/` from their actual storage locations outside the project, preserving symlinks; keep `.storage.json` if present. Backups contain login credentials.

**Upgrade:** stop, back up, and save local changes, then run:

```bash
git pull --ff-only
.venv/bin/pip install -r requirements.txt
./start.sh    # Include the original --storage-dir for custom storage
./status.sh
```

Startup applies supported database updates; do not recreate the administrator. If Nginx configuration changes, reinstall it and test/reload Nginx. Reinstall the client wheel in training environments when it changes.

**Move an entire deployment:** stop and copy complete `instance/` and `data/` directories to an empty deployment on the new machine, preserving symlinks. Deploy the same code version, recreate `.venv`, configure Nginx, and start with the appropriate storage options. Startup adapts data paths and stale PID records. Keep the original `manager.db` and `secret.key`; do not overwrite an existing deployment.

**Move one user:** stop their personal instance and owned team instances on the source, export, then copy the complete backup directory to the target deployment and import:

```bash
# Source
.venv/bin/flask --app app export-user alice /tmp/alice-backup
# Target
.venv/bin/flask --app app import-user /tmp/alice-backup
```

The backup contains `user.json`, account credentials, personal data, and team projects created by that user. Import refuses existing accounts or conflicting data, restores a regular user, and requires inviting team members again. Symlinks outside the user's directory are rejected. After migration, update training server addresses and handle external paths and files separately.

**Uninstall:** stop and make any required backup, remove `/etc/nginx/conf.d/trackio-manager.conf`, test/reload Nginx, then delete the project directory. Remove external storage and backups separately if desired.

## UI resources

Reusable components, themes, and layouts live in [`tracknest-ui/`](tracknest-ui/). See its [`SKILL.md`](tracknest-ui/SKILL.md) for reuse instructions.
