---
name: setup
description: "Use when a session is about to make its first vibesys-cli call (vibesys:create-task and vibesys:optimize start here), when vibesys-cli is missing, fails or reports a version the skills do not support, or when the user asks to install, upgrade or check it. Finds vibesys-cli or installs it with the user's approval, upgrades it only to a release the skills support, checks its version against the range the skills declare, and exports its absolute path as VIBESYS_CLI for the session."
---

# Set up

You prepare the environment the vibesys skills run in. Today that is one binary, `vibesys-cli`:
select one working build, check that the skills support its version, and export its absolute path
as `VIBESYS_CLI` for this session and every command you delegate. Never edit shell profiles.

The skills support the versions compatible with the minimum in `assets/min-cli-version`, by
caret rules: from 1.0 on, the same major version and at least the minimum; before 1.0, the same
minor version and at least the minimum. A pre-release (`0.1.0-rc.2`) is older than its release.
Never do this arithmetic yourself: `scripts/cli_version.sh` (POSIX sh, needs curl or wget only
to list releases) does it. Script paths: read `references/harness/<harness>.md` for the harness
you are running in. Below, `cli_version.sh` stands for that path, always run with `sh`.

## Check the version

Run `sh cli_version.sh check "$VIBESYS_CLI"`. It prints one line starting with the status and
exits with:

| Exit | Status | Do |
| --- | --- | --- |
| 0 | `ok` | Continue. |
| 3 | `too-old` | Tell the user and offer to [upgrade](#upgrade). Without approval, stop. |
| 4 | `too-new` | The CLI is newer than these skills support. Tell the user, and ask them to update the vibesys skills or to approve a reinstall of the newest supported version ([Install](#install), a downgrade). Without either, stop. |
| 5 | `dev` | A development build has no version to check. Say once that it is unchecked, then continue with it. |
| 6 | `unknown` | The binary did not run or printed no readable version. Treat it as broken: report the line and the binary's own `--version` output. |
| 1 or 2 | | The helper itself failed. Report its message and stop. |

Run the check whenever you select a binary, and again after every install and upgrade. Never
continue on an `unknown` binary, or on a `too-old` or `too-new` one the user did not accept.

## Find

1. If `VIBESYS_CLI` is set, it was chosen by the user or the orchestrator. It must be an
   absolute path; check it. Reuse it when the check says `ok` or `dev`. Otherwise report the
   problem and ask whether to repair or upgrade that installation or select another build. Never
   silently replace an explicitly selected or orchestrator-provided binary.
2. If it is unset, look for `vibesys-cli` on `PATH`, then at
   `${VIBESYS_CLI_INSTALL_DIR:-$HOME/.local/bin}/vibesys-cli`. Resolve the first one that runs
   to an absolute path, `export VIBESYS_CLI=<that path>`, and check it.
3. If none runs, [install](#install) one.

## Install

1. Ask the user whether you may download and install `vibesys-cli` from the official GitHub
   releases into `${VIBESYS_CLI_INSTALL_DIR:-$HOME/.local/bin}`. Explain that the installer
   checks the release's SHA-256 and needs no root access. Wait for explicit approval. If it is
   declined or unanswered, stop without installing or starting work.
2. Pick the version: the one the user named, or else `V=$(sh cli_version.sh newest-compatible)`.
   If that fails (no network, or no stable release is supported), report its message and stop.
3. Install exactly that version. Keep any other installer settings the user has
   (`VIBESYS_CLI_INSTALL_DIR`, `VIBESYS_CLI_RELEASES_URL`):

   ```sh
   curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/cli/install.sh | VIBESYS_CLI_VERSION="$V" sh
   ```

   Always pin `VIBESYS_CLI_VERSION`: without it the installer takes the newest stable release,
   which may be outside the supported range.
4. Export the installed binary's absolute path as `VIBESYS_CLI` and check it. If the install or
   the check fails, report the error and stop; never continue with an unverified binary.
   Otherwise continue the requested task without asking the user to restart.

## Upgrade

Upgrade only when the user asked for it, or approved it after a `too-old` check. Upgrading a
`dev` build replaces a developer's own build: do it only when the user asks by name.

1. Find the newest stable release, `N=$(sh cli_version.sh newest)`, and the newest one the
   skills support, `C=$(sh cli_version.sh newest-compatible)`. If either fails, report and stop.
2. If `N` equals `C`, run `"$VIBESYS_CLI" upgrade`. It takes no arguments, always moves to the
   newest stable release, and replaces the binary in place.
3. Otherwise the newest release is outside the supported range, so never run `upgrade`.
   Reinstall `C` over the current binary instead, with the installer pinned:
   `curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/cli/install.sh | VIBESYS_CLI_VERSION="$C" VIBESYS_CLI_INSTALL_DIR="$(dirname "$VIBESYS_CLI")" sh`.
4. If the upgrade or install says the binary's directory is not writable, ask the user whether to
   install `C` into a directory they own ([Install](#install)) and switch `VIBESYS_CLI` to it.
5. Check the version again. If it is not `ok`, report and stop.

## Hand off

Reuse the selected `VIBESYS_CLI` for the rest of the session and pass its absolute path to every
subagent and background command. Tell the user which path and version you selected, and the
check's status line.
