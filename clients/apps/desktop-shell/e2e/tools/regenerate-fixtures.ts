/**
 * Rewrites the committed run fixtures (`libs/run-fixtures/src/data/*.json`): the synthetic
 * scenarios from their generators, and `queue-rs-live` by recording this app's fake run through the
 * same recorder a real run's journal goes through. Run it after changing a scenario or the fake
 * run: `pnpm --filter @vibesys/app-desktop-shell fixtures`. A unit test in the fixtures package
 * fails when a data file differs from its generator.
 */
import {mkdirSync, writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {NO_NAMES, type RunFixture, recordFixture, scenarios} from '@vibesys/run-fixtures';
import {judgeEvents, traceEvents, turnEvents} from '../fake-trace.js';

const DATA = fileURLToPath(new URL('../../../../libs/run-fixtures/src/data/', import.meta.url));

function write(fixture: RunFixture): void {
  writeFileSync(`${DATA}${fixture.name}.json`, `${JSON.stringify(fixture, null, 2)}\n`);
  process.stdout.write(`wrote ${fixture.name}.json (${fixture.events.length} events)\n`);
}

/** The fake run's whole journal: its trace, W5's turn and the judge's reply. */
function fakeRunJournal(): string {
  const spans = traceEvents();
  const turn = turnEvents(spans.length);
  const judge = judgeEvents(spans.length + turn.length);
  return [...spans, ...turn, ...judge].map(event => JSON.stringify(event)).join('\n');
}

mkdirSync(DATA, {recursive: true});
for (const fixture of scenarios()) write(fixture);
write(
  recordFixture({
    name: 'queue-rs-live',
    description:
      'The desktop shell fake run: a dynamic search over two planning calls, seven ideas, W5 with a turn and a judge.',
    journal: fakeRunJournal(),
    state: 'live',
    objective: {
      text: 'Maximize trusted end-to-end throughput of the MPMC queue.',
      metric: 'total_ops_per_sec',
      unit: 'ops/s',
      direction: 'max',
    },
    policy: NO_NAMES,
  }),
);
