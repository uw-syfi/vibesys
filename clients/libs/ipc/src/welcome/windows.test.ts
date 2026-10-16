import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {runOfWindow, runWindowUrl} from './windows.js';

const HOSTS = ['local', 'ssh:gpu-box', 'ssh:user@cluster.example', 'ssh:a.b_c-d'];
const NAME_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789._-';

function runId(random: Seeded): string {
  const first = NAME_CHARS[random.int(36)] ?? 'a';
  const rest = Array.from({length: random.int(40)}, () => NAME_CHARS[random.int(39)] ?? 'a');
  return [first, ...rest].join('');
}

describe('run window URLs', () => {
  test('property: the page a run window loads names exactly that run', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const run = {host: HOSTS[random.int(HOSTS.length)] ?? 'local', runId: runId(random)};
      const url = runWindowUrl('app://vibesys/shell.html', run);
      expect(url.startsWith('app://vibesys/shell.html?')).toBe(true);
      expect(runOfWindow(url.slice(url.indexOf('?')))).toEqual(run);
    }
  });

  test('the main window has no run; a malformed query names what is wrong', () => {
    expect(runOfWindow('')).toBeNull();
    expect(runOfWindow('?')).toBeNull();
    expect(() => runOfWindow('?host=local&run=r1&tab=logs')).toThrow('"tab" is not a known');
    expect(() => runOfWindow('?host=local&host=local&run=r1')).toThrow('host is given twice');
    expect(() => runOfWindow('?host=local')).toThrow('run window.runId must be a run id');
    expect(() => runOfWindow('?host=nowhere&run=r1')).toThrow('run window.host');
    expect(() => runOfWindow('?host=local&run=Bad%20Id')).toThrow('runId must be a run id');
  });
});
