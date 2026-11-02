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
    const child = spawn('ssh', ['-o', 'BatchMode=yes', host, 'vibesys-orchestrator', 'connect'], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let lastWords = '';
    child.stderr.on('data', (chunk: Buffer) => {
      lastWords = chunk.toString('utf8').trim().split('\n').pop() ?? lastWords;
    });
    return {
      write: bytes => void child.stdin.write(bytes),
      onData: listener => void child.stdout.on('data', (chunk: Buffer) => listener(chunk)),
      onClose: listener => void child.on('close', code => listener(lastWords || `exit ${code}`)),
      kill: () => void child.kill(),
    };
  };
}
