import {expect, test} from 'bun:test';
import {parseCampaignFrame} from './campaign-frames.js';

const validWorkstream = {
  id: 'ws-1',
  title: 'Prefix reuse',
  hypothesis: 'Reuse prefixes',
  startedAt: '2026-01-01T00:00:00Z',
  firstSequence: 1,
  phase: 'implementing',
  active: true,
  finishedAt: null,
  lastSequence: null,
  outcome: null,
  outcomeSummary: '',
};

test('parses every frame kind', () => {
  expect(parseCampaignFrame({kind: 'campaign-init', seq: 1, header: {}}).kind).toBe(
    'campaign-init',
  );
  expect(parseCampaignFrame({kind: 'agent-upsert', seq: 2, agent: {}}).kind).toBe('agent-upsert');
  expect(
    parseCampaignFrame({kind: 'workstream-upsert', seq: 3, workstream: validWorkstream}).kind,
  ).toBe('workstream-upsert');
  expect(parseCampaignFrame({kind: 'measurement', seq: 4, measurement: {}}).kind).toBe(
    'measurement',
  );
  expect(parseCampaignFrame({kind: 'tokens', seq: 5, workstreamId: 'ws-1', tokens: 10}).kind).toBe(
    'tokens',
  );
  expect(parseCampaignFrame({kind: 'status', seq: 6, status: 'completed'}).kind).toBe('status');
});

test('rejects an unknown frame kind and names the path', () => {
  expect(() => parseCampaignFrame({kind: 'nope', seq: 1})).toThrow(/\$\.kind/);
});

test('rejects an unexpected key and names it', () => {
  expect(() => parseCampaignFrame({kind: 'status', seq: 1, status: 'active', extra: true})).toThrow(
    /\$\.extra/,
  );
});

test('rejects a missing key and names it', () => {
  expect(() => parseCampaignFrame({kind: 'status', seq: 1})).toThrow(/\$\.status/);
});

test('rejects a wrong-typed seq and names it', () => {
  expect(() => parseCampaignFrame({kind: 'status', seq: '1', status: 'active'})).toThrow(/\$\.seq/);
});

test('rejects an invalid workstream phase and names the nested path', () => {
  expect(() =>
    parseCampaignFrame({
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, phase: 'bogus'},
    }),
  ).toThrow(/\$\.workstream\.phase/);
});

test('rejects an unexpected workstream key and names the nested path', () => {
  expect(() =>
    parseCampaignFrame({
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, surprise: 1},
    }),
  ).toThrow(/\$\.workstream\.surprise/);
});

test('rejects an invalid status value', () => {
  expect(() => parseCampaignFrame({kind: 'status', seq: 1, status: 'paused'})).toThrow(
    /\$\.status/,
  );
});

test('rejects a non-object frame', () => {
  expect(() => parseCampaignFrame(null)).toThrow(/\$/);
  expect(() => parseCampaignFrame([])).toThrow(/\$/);
});
