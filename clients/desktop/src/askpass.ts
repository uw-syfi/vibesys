/**
 * The askpass helper the SSH master runs (`SSH_ASKPASS`, with `SSH_ASKPASS_REQUIRE=force`) when a
 * host asks for a password, a 2FA code, or a host-key confirmation.
 *
 * It shows ssh's own prompt in a native dialog (AppleScript on macOS, zenity or kdialog on Linux)
 * and prints the answer to its standard output, which only ssh reads. It writes nothing else and
 * logs nothing; a cancelled dialog exits non-zero, which ssh reports as an authentication failure
 * and the app shows as "authentication needed". Questions (`yes/no`) are asked in the clear,
 * everything else with a hidden field.
 */
import {chmod, mkdir, stat, writeFile} from 'node:fs/promises';
import {dirname} from 'node:path';

const ASKPASS_SCRIPT = `#!/bin/sh
# VibeSys askpass helper: shows ssh's prompt in a dialog; the answer goes to ssh only.
prompt=$1
hidden="with hidden answer"
case $prompt in *yes/no*|*'(yes'*) hidden="" ;; esac
if command -v osascript >/dev/null 2>&1; then
  exec osascript \\
    -e 'on run argv' \\
    -e "display dialog (item 1 of argv) default answer \\"\\" $hidden with title \\"VibeSys\\" buttons {\\"Cancel\\", \\"OK\\"} default button \\"OK\\" with icon caution" \\
    -e 'text returned of result' \\
    -e 'end run' \\
    "$prompt" 2>/dev/null
fi
if command -v zenity >/dev/null 2>&1; then
  if [ -n "$hidden" ]; then exec zenity --entry --hide-text --title=VibeSys --text="$prompt" 2>/dev/null; fi
  exec zenity --entry --title=VibeSys --text="$prompt" 2>/dev/null
fi
if command -v kdialog >/dev/null 2>&1; then
  if [ -n "$hidden" ]; then exec kdialog --title VibeSys --password "$prompt" 2>/dev/null; fi
  exec kdialog --title VibeSys --inputbox "$prompt" 2>/dev/null
fi
exit 1
`;

/** Write the helper to `path` (owner-only, executable) and return it. */
export async function installAskpass(path: string): Promise<string> {
  await mkdir(dirname(path), {recursive: true});
  await writeFile(path, ASKPASS_SCRIPT, {mode: 0o700});
  await chmod(path, 0o700);
  return path;
}

/**
 * A private directory for SSH master sockets: short (Unix socket paths are capped near 104 bytes,
 * and `%C` adds 40), owned by this user, and closed to everyone else.
 */
export async function controlDirectory(uid: number): Promise<string> {
  const directory = `/tmp/vsd-${uid}`;
  await mkdir(directory, {recursive: true, mode: 0o700});
  const info = await stat(directory);
  if (info.uid !== uid || (info.mode & 0o077) !== 0) {
    throw new Error(`${directory} must be owned by you and private; remove it and try again`);
  }
  return directory;
}
