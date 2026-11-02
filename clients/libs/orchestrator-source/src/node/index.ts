/**
 * The one place that starts a process: `ssh <host> vibesys-orchestrator connect`, as a
 * `ProcessPort` for `StdioSource`. Its arguments are fixed (no shell, no interpolation): the host
 * is one argument, and options that would let a host name act as a flag are refused.
 */
import {spawn} from 'node:child_process';
import type {LaunchProcess} from '../stdio-source.js';

export function sshLauncher(host: string): LaunchProcess {
  if (host === '' || host.startsWith('-') || /\s/.test(host)) {
    throw new Error(`not a host to connect to: ${JSON.stringify(host)}`);
  }
  return () => {
    // Keepalives end a connection that went quiet (a closed laptop, a dead route) in about a
    // minute, so a stream fails and is redialled instead of waiting forever.
    const child = spawn(
      'ssh',
      [
        '-o',
        'BatchMode=yes',
        '-o',
        'ConnectTimeout=15',
        '-o',
        'ServerAliveInterval=15',
        '-o',
        'ServerAliveCountMax=3',
        host,
        'vibesys-orchestrator',
        'connect',
      ],
      {stdio: ['pipe', 'pipe', 'pipe']},
    );
    let lastWords = '';
    let gone: ((detail: string) => void) | null = null;
    let reported = false;
    const report = (detail: string): void => {
      if (reported) return;
      reported = true;
      gone?.(detail);
    };
    child.stderr.on('data', (chunk: Buffer) => {
      lastWords = chunk.toString('utf8').trim().split('\n').pop() ?? lastWords;
    });
    // A missing ssh program, or a pipe closed under a write, is a lost connection, not a crash.
    child.on('error', error => report(error.message));
    child.stdin.on('error', error => report(lastWords || error.message));
    child.on('close', code => report(lastWords || `exit ${code}`));
    return {
      write: bytes => void child.stdin.write(bytes),
      onData: listener => void child.stdout.on('data', (chunk: Buffer) => listener(chunk)),
      onClose: listener => {
        gone = listener;
        if (reported) listener(lastWords || 'closed');
      },
      kill: () => void child.kill(),
    };
  };
}
