import {describe, expect, test} from 'bun:test';
import {parseInstanceRecord} from './instances.js';
import {suggestCheckout, suggestProjects} from './suggestions.js';
import {fakeRecord} from './testing/fake-record.js';

describe('suggestions', () => {
  const record = (id: string, project: string, root: string | null) =>
    parseInstanceRecord({
      ...fakeRecord(id, '/run/s', 1),
      project_root: project,
      ...(root === null ? {} : {vibesys_root: root}),
    });

  test('the checkout comes from the first record that names one', () => {
    expect(suggestCheckout([])).toBeNull();
    expect(
      suggestCheckout([
        record('0123456789ab', '/p', null),
        record('ba9876543210', '/q', '/home/me/src/vibesys'),
      ]),
    ).toBe('/home/me/src/vibesys');
  });

  test('projects: live ones first, then recent ones on this host, each once', () => {
    const recent = [
      {
        host: 'ssh:gpu',
        project: '/q',
        task: null,
        instanceId: 'aaaaaaaaaaaa',
        runId: null,
        attachedAt: 1,
      },
      {
        host: 'ssh:gpu',
        project: '/r',
        task: null,
        instanceId: 'bbbbbbbbbbbb',
        runId: null,
        attachedAt: 2,
      },
      {
        host: 'local',
        project: '/s',
        task: null,
        instanceId: 'cccccccccccc',
        runId: null,
        attachedAt: 3,
      },
    ];
    expect(suggestProjects([record('0123456789ab', '/q', null)], recent, 'ssh:gpu')).toEqual([
      '/q',
      '/r',
    ]);
  });
});
