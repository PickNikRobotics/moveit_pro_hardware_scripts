# MoveIt Pro Hardware Scripts

Reference installer, systemd unit, and sudoers template for running [MoveIt Pro](https://docs.picknik.ai/) as a service on hardware targeted by a Continuous Deployment pipeline.

The full setup walkthrough lives at [Set Up CI/CD](https://docs.picknik.ai/how_to/computer_configuration/ci_cd_for_objectives/) in the MoveIt Pro docs. This README covers what is in the repo and how to use it directly.

## Contents

- `install.sh` — one-shot installer. Copies the wrapper, systemd unit, and sudoers drop-in into place. Run on each target machine.
- `bin/install-moveit-pro` — root-owned installer wrapper. Validates the version string against a strict regex, downloads the `.deb` to a root-owned cache, installs it, and deletes the file.
- `bin/moveit-pro@.service` — systemd template unit. Runs `moveit_pro run --no-browser` as `%i`. Restarts on failure. Reads optional environment from `/etc/default/moveit-pro`.
- `bin/notify-crash.py` — posts to Slack and opens/updates a GitHub issue via `ExecStopPost` when the service exits non-zero. Reads `SLACK_WEBHOOK_URL` and `MOVEIT_CD_GITHUB_TOKEN` from the environment; each notification is skipped if its variable is unset.
- `bin/notify_lib.py` — shared notification helpers (`slack_post`, `github_issue`) used by both `notify-crash.py` and `cd_objective_lib.py`. Installed to `/usr/lib/moveit-pro-scripts/`. `github_issue` deduplicates by exact title within a label: a repeated failure bumps an occurrence counter and appends a row instead of opening a new issue.
- `bin/ci-runner.sudoers.template` — sudoers drop-in. `install.sh` substitutes `__CI_USER__` with the local account and installs at `/etc/sudoers.d/<user>-ci`. Grants NOPASSWD on the installer and the user's own systemd unit only.
- `example_scripts/cd_objective_lib.py` — helper library for sending an Objective goal via rosbridge, used by the example scripts. On objective timeout or rosbridge failure it posts to Slack, opens/updates a GitHub issue, and stops the systemd unit (via `notify_lib.py`).
- `example_scripts/3-waypoint-pick-and-place.py`, `example_scripts/ml-segment-image.py`, `example_scripts/move-all-boxes.py` — example smoke-test scripts that drive an Objective on `localhost:3201` rosbridge.

## Install

On the target machine:

```bash
git clone https://github.com/PickNikRobotics/moveit_pro_hardware_scripts.git
cd moveit_pro_hardware_scripts
sudo ./install.sh
```

This installs:

- The objective scripts to `/usr/bin/`.
- `cd_objective_lib.py` and `notify_lib.py` to `/usr/lib/moveit-pro-scripts/`.
- `notify-crash.py` to `/usr/bin/`.
- `install-moveit-pro` to `/usr/local/sbin/` (root-owned, `0755`).
- `/var/cache/moveit-pro/` as a root-owned download cache.
- `moveit-pro@.service` and `virtual-screen.service` to `/etc/systemd/system/`.
- `/etc/sudoers.d/<user>-ci` (validated with `visudo -cf`) granting NOPASSWD on the installer and `systemctl restart`/`stop` of the user's own service unit.

The install script enables — but does not start — the MoveIt Pro service for the current user.

### Optional: per-machine workspace override

`install-moveit-pro` reads `/etc/moveit-pro-cd.conf` (if present, root-owned) to pick the workspace repo cloned on each CD run. Without a config file, it clones [moveit_pro_example_ws](https://github.com/PickNikRobotics/moveit_pro_example_ws) pinned to the release version. Pass `--config` to `install.sh` to lay down a per-machine override:

```bash
sudo ./install.sh --config moveit-pro-cd.<machine>.conf
```

Schema:

```bash
# Public example_ws pinned to release (default — equivalent to no file):
WORKSPACE_REPO=https://github.com/PickNikRobotics/moveit_pro_example_ws.git
WORKSPACE_DIR=moveit_pro_example_ws
WORKSPACE_PIN_TO_RELEASE=true

# Private workspace on a fixed branch:
WORKSPACE_REPO=git@github.com:<owner>/<repo>.git
WORKSPACE_DIR=<repo>
WORKSPACE_BRANCH=main
WORKSPACE_PIN_TO_RELEASE=false
```

`WORKSPACE_REPO` is regex-restricted to `https://github.com/<owner>/<repo>.git` or `git@github.com:<owner>/<repo>.git`. For the SSH form, the CI user needs a deploy key with read-only access.

### Optional: failure notifications (Slack + GitHub issues)

Both notifiers read their config from `/etc/default/moveit-pro` (root-owned). The systemd unit loads this file via `EnvironmentFile=`, so `notify-crash.py` and `cd_objective_lib.py` pick it up for crash and CD-failure events. Each notifier is independent: set only the variables you want.

```bash
sudo install -m 0640 -o root -g root /dev/stdin /etc/default/moveit-pro <<'EOF'
# Slack incoming webhook. Unset -> Slack skipped.
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ

# GitHub issue on failure. Unset -> issue creation skipped.
MOVEIT_CD_GITHUB_TOKEN=github_pat_xxx
# Optional overrides (defaults shown):
# MOVEIT_CD_ISSUE_REPO=PickNikRobotics/moveit_pro
# MOVEIT_CD_ISSUE_LABEL=qa-deployment-failure
EOF
```

If a variable is unset, that notification is silently skipped — this is how non-QA machines opt out of issue creation.

`MOVEIT_CD_GITHUB_TOKEN` must be a **fine-grained PAT scoped to the issue repo with `Issues: Read and write` and nothing else** — the narrowest credential that can file an issue. Do not grant `Contents` or any other scope: a QA machine is a higher-exposure host, and the token only needs to open and comment on issues. The `qa-deployment-failure` label must already exist on the repo (the API does not create labels on demand).

Repeated failures of the same kind on the same machine deduplicate to a single issue (matched by title within the label) — each recurrence bumps an occurrence counter, appends a table row with the version/time/reason, and adds a comment for visibility.

## Verify the install

```bash
# Sudo without password
sudo -n /usr/local/sbin/install-moveit-pro 9.4.0

# Start the service
sudo systemctl start moveit-pro@$USER.service

# Status / logs
systemctl status moveit-pro@$USER.service
journalctl -u moveit-pro@$USER.service -e
```

A password prompt on the first command means the sudoers drop-in did not land. Re-run `install.sh` and check `sudo visudo -c`.

## CD pipeline

The CI runner SSHes into each target machine over a mesh VPN (Tailscale, WireGuard, or any other) and runs three commands in order:

1. `sudo -n /usr/local/sbin/install-moveit-pro <version>` — downloads and installs the `.deb`.
2. `sudo -n /bin/systemctl restart moveit-pro@<user>.service` — restarts the service.
3. `/usr/bin/<objective>.py` — optional smoke test of an Objective via rosbridge.

The sudoers drop-in grants NOPASSWD on **only** steps 1 and 2. The installer validates the version string with a strict regex and downloads to a root-owned path, so a compromised CI account cannot escalate by planting a malicious `.deb`.

See [Set Up CI/CD](https://docs.picknik.ai/how_to/computer_configuration/ci_cd_for_objectives/) for the full pipeline, a sample GitHub Actions workflow, and the security model.

## Licence

BSD 3-Clause. See [LICENSE](LICENSE).
