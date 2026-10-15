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
import {randomUUID} from 'node:crypto';
import {lstat, mkdir, open, rename, unlink} from 'node:fs/promises';
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

/** Atomically install the helper as an owner-only executable and return its path. */
export async function installAskpass(path: string): Promise<string> {
  await mkdir(dirname(path), {recursive: true});
  const staging = await stagePrivateText(path, ASKPASS_SCRIPT, 0o700);
  try {
    await rename(staging, path);
  } catch (error) {
    // Best effort: a failed cleanup must not hide the reason the replace itself failed.
    await unlink(staging).catch(() => {});
    throw error;
  }
  return path;
}

const MAX_STAGING_ATTEMPTS = 8;

/** Create a complete private regular file without ever opening an existing path. */
async function stagePrivateText(target: string, text: string, mode: number): Promise<string> {
  let collision: unknown;
  for (let attempt = 0; attempt < MAX_STAGING_ATTEMPTS; attempt += 1) {
    const staging = `${target}.${randomUUID()}.tmp`;
    try {
      await writeNewPrivateText(staging, text, mode);
      return staging;
    } catch (error) {
      if (!hasCode(error, 'EEXIST')) throw error;
      collision = error;
    }
  }
  throw new Error(`could not create a private staging file beside ${target}`, {cause: collision});
}

async function writeNewPrivateText(path: string, text: string, mode: number): Promise<void> {
  const file = await open(path, 'wx', mode);
  try {
    await file.writeFile(text);
    // Creation mode is umask-sensitive; chmod the owned descriptor to make the contract exact.
    await file.chmod(mode);
    await file.close();
  } catch (error) {
    await file.close().catch(() => {});
    await unlink(path).catch(() => {});
    throw error;
  }
}

function hasCode(error: unknown, code: string): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === code;
}

/** What the filesystem proved about the control path entry itself, without following it. */
interface ControlDirectoryEntry {
  readonly kind: 'directory' | 'symbolic-link' | 'other';
  readonly uid: number;
  readonly mode: number;
}

/** The filesystem boundary used to create and inspect the SSH control directory. */
export interface ControlDirectoryFilesystem {
  createPrivateDirectory(path: string): Promise<void>;
  inspectEntry(path: string): Promise<ControlDirectoryEntry>;
}

const NODE_CONTROL_DIRECTORY_FILESYSTEM: ControlDirectoryFilesystem = {
  createPrivateDirectory: async path => {
    await mkdir(path, {mode: 0o700});
  },
  inspectEntry: async path => {
    const info = await lstat(path);
    return {
      kind: info.isDirectory() ? 'directory' : info.isSymbolicLink() ? 'symbolic-link' : 'other',
      uid: info.uid,
      mode: info.mode,
    };
  },
};

/**
 * A private directory for SSH master sockets: short (Unix socket paths are capped near 104 bytes,
 * and `%C` adds 40), owned by this user, and closed to everyone else.
 */
export async function controlDirectory(
  uid: number,
  filesystem: ControlDirectoryFilesystem = NODE_CONTROL_DIRECTORY_FILESYSTEM,
): Promise<string> {
  const directory = `/tmp/vsd-${uid}`;
  try {
    await filesystem.createPrivateDirectory(directory);
  } catch (error) {
    if (!(error instanceof Error && 'code' in error && error.code === 'EEXIST')) {
      throw error;
    }
  }
  const entry = await filesystem.inspectEntry(directory);
  if (entry.kind !== 'directory' || entry.uid !== uid || (entry.mode & 0o077) !== 0) {
    throw new Error(
      `${directory} must be a real directory owned by you and private; remove it and try again`,
    );
  }
  return directory;
}
