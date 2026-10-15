import {describe, expect, test} from 'bun:test';
import {controlDirectory} from '../index.js';

const UID = 1_004;
const PATH = `/tmp/vsd-${UID}`;

function filesystem(
  entry: {
    readonly kind: 'directory' | 'symbolic-link' | 'other';
    readonly uid: number;
    readonly mode: number;
  },
  creationError?: Error,
) {
  const calls: string[] = [];
  return {
    calls,
    port: {
      async createPrivateDirectory(path: string): Promise<void> {
        calls.push(`create:${path}`);
        if (creationError) throw creationError;
      },
      async inspectEntry(path: string): Promise<typeof entry> {
        calls.push(`inspect:${path}`);
        return entry;
      },
    },
  };
}

describe('SSH control directory', () => {
  test('accepts only the exact private directory owned by the user', async () => {
    const accepted = filesystem({kind: 'directory', uid: UID, mode: 0o40700});

    await expect(controlDirectory(UID, accepted.port)).resolves.toBe(PATH);
    expect(accepted.calls).toEqual([`create:${PATH}`, `inspect:${PATH}`]);

    const invalid = [
      {kind: 'symbolic-link', uid: UID, mode: 0o40700},
      {kind: 'other', uid: UID, mode: 0o40700},
      {kind: 'directory', uid: UID + 1, mode: 0o40700},
      {kind: 'directory', uid: UID, mode: 0o40750},
      {kind: 'directory', uid: UID, mode: 0o40707},
    ] as const;
    for (const entry of invalid) {
      const rejected = filesystem(entry);
      await expect(controlDirectory(UID, rejected.port)).rejects.toThrow(
        `${PATH} must be a real directory owned by you and private`,
      );
      expect(rejected.calls).toEqual([`create:${PATH}`, `inspect:${PATH}`]);
    }
  });

  test('inspects an existing path before deciding whether it is safe', async () => {
    const alreadyExists = Object.assign(new Error('already exists'), {code: 'EEXIST'});
    const accepted = filesystem({kind: 'directory', uid: UID, mode: 0o40700}, alreadyExists);

    await expect(controlDirectory(UID, accepted.port)).resolves.toBe(PATH);
    expect(accepted.calls).toEqual([`create:${PATH}`, `inspect:${PATH}`]);

    const rejected = filesystem({kind: 'other', uid: UID, mode: 0o40700}, alreadyExists);
    await expect(controlDirectory(UID, rejected.port)).rejects.toThrow(
      `${PATH} must be a real directory owned by you and private`,
    );
    expect(rejected.calls).toEqual([`create:${PATH}`, `inspect:${PATH}`]);
  });

  test('propagates directory creation failures without inspecting the path', async () => {
    const denied = Object.assign(new Error('permission denied'), {code: 'EACCES'});
    const unavailable = filesystem({kind: 'directory', uid: UID, mode: 0o40700}, denied);

    await expect(controlDirectory(UID, unavailable.port)).rejects.toBe(denied);
    expect(unavailable.calls).toEqual([`create:${PATH}`]);
  });
});
