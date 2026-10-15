import {describe, expect, test} from 'bun:test';
import {lstat, mkdir, mkdtemp, readdir, readFile, rm, symlink, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {controlDirectory, installAskpass} from '../index.js';

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

describe('SSH askpass installer', () => {
  test('replaces a destination symlink without changing its target', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'vsd-askpass-'));
    const target = join(directory, 'unrelated');
    const askpass = join(directory, 'askpass.sh');
    try {
      await writeFile(target, 'keep me\n', {mode: 0o644});
      const targetMode = (await lstat(target)).mode & 0o777;
      await symlink(target, askpass);

      await expect(installAskpass(askpass)).resolves.toBe(askpass);

      const installed = await lstat(askpass);
      expect(installed.isFile()).toBeTrue();
      expect(installed.isSymbolicLink()).toBeFalse();
      expect(installed.mode & 0o777).toBe(0o700);
      expect(await readFile(askpass, 'utf8')).toStartWith('#!/bin/sh\n');
      expect(await readFile(target, 'utf8')).toBe('keep me\n');
      expect((await lstat(target)).mode & 0o777).toBe(targetMode);
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });

  test('atomically replaces a regular helper with exact executable permissions', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'vsd-askpass-'));
    const askpass = join(directory, 'askpass.sh');
    try {
      await writeFile(askpass, 'old helper\n', {mode: 0o600});

      await installAskpass(askpass);

      const installed = await lstat(askpass);
      expect(installed.isFile()).toBeTrue();
      expect(installed.mode & 0o777).toBe(0o700);
      expect(await readFile(askpass, 'utf8')).toStartWith('#!/bin/sh\n');
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });

  test('a failed destination replace leaves no owned staging file', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'vsd-askpass-'));
    const askpass = join(directory, 'askpass.sh');
    try {
      await mkdir(askpass);

      await expect(installAskpass(askpass)).rejects.toThrow();

      expect(await readdir(askpass)).toEqual([]);
      expect((await readdir(directory)).filter(name => name.endsWith('.tmp'))).toEqual([]);
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });
});
