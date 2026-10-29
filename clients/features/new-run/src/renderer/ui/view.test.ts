import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {harnessMark, projectLabels, type Readiness, readinessStatus} from './view.js';

const STATES = ['ok', 'warn', 'fail', 'pending'] as const;

function items(random: Seeded): Readiness[] {
  return Array.from({length: random.int(8)}, (_, index) => ({
    id: `check-${index}`,
    state: random.pick(STATES),
    label: `Check ${index}`,
    detail: `Detail ${index}`,
  }));
}

describe('readinessStatus', () => {
  test('is open exactly when a check fails, and then says how many', () => {
    for (const seed of SEEDS) {
      const checks = items(new Seeded(seed));
      const failed = checks.filter(item => item.state === 'fail').length;
      const status = readinessStatus(checks);
      expect(status.open).toBe(failed > 0);
      expect(status.state === 'fail').toBe(failed > 0);
      if (failed > 0) expect(status.text).toContain(`${failed} check`);
    }
  });

  test('says ready with the count of what passed when nothing fails or waits', () => {
    for (const seed of SEEDS) {
      const checks = items(new Seeded(seed)).filter(
        item => item.state === 'ok' || item.state === 'warn',
      );
      const status = readinessStatus(checks);
      expect(status.open).toBe(false);
      expect(status.text.startsWith('Ready')).toBe(true);
      const passed = checks.filter(item => item.state === 'ok').length;
      if (passed > 0) expect(status.text).toContain(`${passed} check`);
    }
  });

  test('names what is being waited for when nothing fails', () => {
    const status = readinessStatus([
      {id: 'a', state: 'ok', label: 'A'},
      {id: 'b', state: 'pending', label: 'Project', detail: 'Choose a project.'},
    ]);
    expect(status).toEqual({state: 'pending', text: 'Choose a project.', open: false});
  });

  test('one check reads in the singular', () => {
    expect(readinessStatus([{id: 'a', state: 'ok', label: 'A'}]).text).toBe(
      'Ready · 1 check passed',
    );
  });
});

describe('projectLabels', () => {
  const paths = (random: Seeded): string[] =>
    [
      ...new Set(
        Array.from({length: 1 + random.int(6)}, () =>
          [
            '',
            'home',
            random.pick(['a', 'b', 'c']),
            random.pick(['x', 'y']),
            random.pick(['q', 'r']),
          ]
            .slice(0, 2 + random.int(4))
            .join('/'),
        ),
      ),
    ].filter(path => path.split('/').filter(Boolean).length > 0);

  test('gives every distinct path its own label, which ends in the folder name', () => {
    for (const seed of SEEDS) {
      const all = paths(new Seeded(seed));
      const labels = projectLabels(all);
      expect(new Set(labels.values()).size).toBe(all.length);
      for (const path of all) {
        const name = path.split('/').filter(Boolean).at(-1) ?? path;
        const label = labels.get(path) ?? '';
        expect(label === path || label.startsWith(name)).toBe(true);
      }
    }
  });

  test('a name used once is the folder alone; a shared one is told apart by the folders above', () => {
    const labels = projectLabels(['/w/queue', '/p/queue', '/w/kv']);
    expect(labels.get('/w/kv')).toBe('kv');
    expect(labels.get('/w/queue')).toBe('queue (w)');
    expect(labels.get('/p/queue')).toBe('queue (p)');
  });
});

describe('names', () => {
  test('a harness without a mark in the package has none', () => {
    expect(harnessMark('claude')).toBe('claudecode');
    expect(harnessMark('codex')).toBeNull();
    expect(harnessMark('something-new')).toBeNull();
  });
});
