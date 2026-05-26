# Quick Start: Manual Deploy

For a machine that has already run `sudo ./install.sh`. Use this to manually upgrade (or downgrade) to any existing MoveIt Pro version.

## Prerequisites

- `install.sh` has already been run on the machine (sudoers drop-in, systemd unit, and `install-moveit-pro` are in place).
- You know the version string. Format: `MAJOR.MINOR.PATCH` or `MAJOR.MINOR.PATCH-rcN` (e.g. `9.2.0`, `9.2.0-rc6`).
- The `.deb` exists at `https://download.picknik.ai/moveit-pro/moveit-pro-<version>-any.deb`.

## Steps

SSH into the machine, then run:

```bash
# 1. Install the version (downloads, installs, deletes the .deb).
sudo -n /usr/local/sbin/install-moveit-pro <version>

# 2. Restart the service so it picks up the new install.
sudo -n systemctl restart moveit-pro@$USER.service

# 3. Confirm it came up cleanly.
systemctl status moveit-pro@$USER.service
journalctl -u moveit-pro@$USER.service -e
```

Replace `<version>` with the target version (e.g. `9.2.0-rc6`). Downgrades work — the installer passes `--allow-downgrades` to apt.

## Optional: run an objective

Each example script sends one goal and exits; the Behavior Tree itself loops internally.

```bash
/usr/bin/3-waypoint-pick-and-place.py
/usr/bin/ml-segment-image.py
/usr/bin/move-all-boxes.py
```

To wrap your own objective into a forever-loop driver, copy `example_scripts/cd_objective_lib.py` and call `run_objectives_forever([...])` with the Objective names you want to chain.

## Troubleshooting

- `sudo -n` prompts for password → sudoers drop-in missing. Re-run `sudo ./install.sh`.
- `Invalid version format` → version string does not match `X.Y.Z` or `X.Y.Z-rcN`.
- 404 from curl → version does not exist on `download.picknik.ai`.
- Service fails to start → check `journalctl -u moveit-pro@$USER.service -e`. If `SLACK_WEBHOOK_URL` is set in `/etc/default/moveit-pro`, `notify-crash.py` will also post to Slack.
