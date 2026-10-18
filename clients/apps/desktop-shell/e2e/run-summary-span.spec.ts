import {readFileSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import type {RunFixture} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

const source = JSON.parse(
  readFileSync(
    new URL('../../../../tests/conformance/fixtures/run-summary/two-ideas.json', import.meta.url),
    'utf8',
  ),
) as {events: RunFixture['events']; summary: Record<string, unknown>};
const tally = (value: Record<string, number | null>) => ({
  input: value['input'],
  cacheRead: value['cache_read'],
  cacheWrite: value['cache_write'],
  output: value['output'],
  reasoning: value['reasoning'],
});
const raw = source.summary as {
  through_sequence: number;
  event_count: number;
  overview: unknown;
  spans: readonly unknown[];
  replies: readonly Record<string, unknown>[];
  tokens: Record<string, number | null>;
  usage: readonly {span_id: string; tally: Record<string, number | null>}[];
};
const summary = {
  throughSequence: raw.through_sequence,
  eventCount: raw.event_count,
  journalBytes: 1234,
  overview: raw.overview,
  spans: raw.spans,
  replies: raw.replies.map(reply => ({
    spanId: reply['span_id'],
    kind: reply['kind'],
    jsonSchema: reply['json_schema'],
    value: reply['value'],
  })),
  tokens: tally(raw.tokens),
  usage: raw.usage.map(entry => ({
    spanId: entry.span_id,
    tokens: tally(entry.tally),
  })),
};
const fixture: RunFixture = {
  format: 1,
  name: 'summary-span',
  description: 'A completed two-idea run with summary and selected span detail.',
  runId: '20261010-120000-11111111-dynamic',
  state: 'finished',
  objective: {text: 'Make the queue faster.', metric: 'ops_per_s', unit: 'ops/s', direction: 'max'},
  events: source.events,
  end: {outcome: 'adopted', reason: 'Adopted the second idea.'},
};

test('summary opens without history and a step fetches only its span', async ({page}) => {
  await openFixture(page, fixture, {summaryServing: {summary, holdSpan: true}});
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toBeVisible();
  const calls = () =>
    page.evaluate(
      () => (window as unknown as {fakeHost: {historyCalls: string[]}}).fakeHost.historyCalls,
    );
  expect(await calls()).toEqual(['summary']);

  await page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').first().click();
  const steps = page.getByRole('listbox', {name: /steps$/});
  await expect(steps).toBeVisible();
  await steps
    .getByRole('option', {name: /implementer/})
    .first()
    .click();
  await expect.poll(calls).toContain('span:op:000002/implementer.1');
  await expect(steps).toBeVisible();
  await expect(page.getByText('Reading step transcript…')).toBeVisible();
  await page.evaluate(() =>
    (window as unknown as {fakeHost: {releaseSpan: () => void}}).fakeHost.releaseSpan(),
  );
  await expect(page.getByText('Reading step transcript…')).toHaveCount(0);
  await expect(steps).toBeVisible();
  expect(await calls()).not.toContain('history');
});
