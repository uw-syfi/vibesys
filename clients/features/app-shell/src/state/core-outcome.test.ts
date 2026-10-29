/** Sidebar and card render the same schema-owned outcome as the run page's replay corpus. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {digestLook, type RunDigestEntry} from '@vibesys/hosts';
import {digestKey, recentRows, SIDEBAR_EMPTY} from './index.js';

const root = new URL('../../../../../proto/testdata/performance/cases.json.gz', import.meta.url);
const cases: readonly {
  name: string;
  presentation: {word: string};
  card: {
    ending: {outcome: 'failed' | 'stopped' | null; summary: string};
    closing: 'finished' | 'failed' | 'interrupted';
    ideas: number;
    adopted_ratio: number | null;
    duration_s: number | null;
  };
}[] = JSON.parse(gunzipSync(readFileSync(root)).toString());
for (const item of cases) {
  test(`sidebar, card and page outcome agree: ${item.name}`, () => {
    const run = {host: 'local' as const, runId: 'run-a'};
    const entry: RunDigestEntry = {
      kind: 'read',
      createdAt: 1,
      ideas: item.card.ideas,
      adoptedRatio: item.card.adopted_ratio,
      bestSoFarRatio: null,
      durationSeconds: item.card.duration_s,
      outcome: item.card.ending.outcome,
      closing: item.card.closing,
      over: true,
      endSummary: item.card.ending.summary,
    };
    const row = recentRows({
      ...SIDEBAR_EMPTY,
      recent: [
        {
          ...run,
          hostLabel: 'Local',
          project: '/project',
          task: null,
          instanceId: null,
          attachedAt: 1,
        },
      ],
      digests: {[digestKey(run)]: {kind: 'read', entry}},
    }).rows[0];
    expect(digestLook(entry)?.label).toBe(item.presentation.word);
    expect(row?.look.word).toBe(item.presentation.word.toLowerCase());
  });
}
