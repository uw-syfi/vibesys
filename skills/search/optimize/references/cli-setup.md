# CLI setup

Use this procedure before task creation or optimization:

1. If `VIBESYS_CLI` is set, run `"$VIBESYS_CLI" --version`. Reuse that exact absolute
   path when it works. If it is relative or fails, report the problem and ask the user
   whether to repair that installation or select another build; never silently replace
   an explicitly selected or orchestrator-provided binary.
2. If unset, look for `vibesys-cli` on `PATH`, then at
   `${VIBESYS_CLI_INSTALL_DIR:-$HOME/.local/bin}/vibesys-cli`. Resolve a working binary
   to an absolute path, verify `--version`, and export it as `VIBESYS_CLI` for this
   session and all delegated commands. Do not edit shell profiles.
3. If no working binary exists, ask the user whether you may download and install
   `vibesys-cli` from the official GitHub releases into
   `${VIBESYS_CLI_INSTALL_DIR:-$HOME/.local/bin}`. Explain that the installer checks
   the release's SHA-256 and needs no root access. Wait for explicit approval; if
   declined or unanswered, stop without installing or starting work.
4. After approval, run
   `curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/cli/install.sh | sh`.
   Respect the user's existing installer settings. Export the installed binary's
   absolute path as `VIBESYS_CLI`, run `"$VIBESYS_CLI" --version`, then continue the
   requested task without asking the user to restart. If installation or verification
   fails, report the error and stop; do not continue with an unverified binary.

Reuse the selected CLI for the rest of the session. Do not upgrade it as part of setup;
`vibesys-cli upgrade` is a separate user-requested action.

