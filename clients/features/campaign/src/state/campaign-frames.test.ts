import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {
  type CampaignFrame,
  type CampaignHeader,
  type CampaignRecord,
  isTerminalPhase,
  parseCampaignFrame,
  parseReplayScenario,
} from './index.js';

const fixturePath = resolve(
  import.meta.dir,
  '../../../../apps/web/dev/fixtures/trajectory-replay.json',
);
const record: CampaignRecord = parseReplayScenario(
  JSON.parse(readFileSync(fixturePath, 'utf8')) as unknown,
);
const validHeader: CampaignHeader = {
  id: record.id,
  title: record.title,
  summary: record.summary,
  provenance: record.provenance,
  objective: record.objective,
  benchmarkVersions: record.benchmarkVersions,
  benchmarkVersionBoundary: record.benchmarkVersionBoundary,
  trajectories: record.trajectories,
};
const validAgent = required(record.agents[0], 'fixture agent');
const validMeasurement = required(
  record.measurements.find(
    measurement => measurement.values.length > 0 && measurement.gates.length > 0,
  ),
  'fixture measurement with values and gates',
);
const validMetric = required(validHeader.objective.metrics[0], 'fixture metric');
const validVersion = required(validHeader.benchmarkVersions[0], 'fixture benchmark version');
const validDefinition = required(validVersion.definitions[0], 'fixture benchmark definition');
const validTrajectory = required(validHeader.trajectories[0], 'fixture trajectory');
const validTurn = required(validTrajectory.turns[0], 'fixture turn');
const validMessage = required(validTurn.messages[0], 'fixture turn message');
const validValue = required(validMeasurement.values[0], 'fixture measurement value');
const validGate = required(validMeasurement.gates[0], 'fixture measurement gate');

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
} as const;

function required<T>(value: T | undefined, label: string): T {
  if (value === undefined) throw new Error(`Expected ${label}`);
  return value;
}

function expectInvalid(frame: unknown, path: string): void {
  expect(() => parseCampaignFrame(frame)).toThrow(path);
}

test('parses every fully populated frame kind', () => {
  expect(parseCampaignFrame({kind: 'campaign-init', seq: 1, header: validHeader}).kind).toBe(
    'campaign-init',
  );
  expect(parseCampaignFrame({kind: 'agent-upsert', seq: 2, agent: validAgent}).kind).toBe(
    'agent-upsert',
  );
  expect(
    parseCampaignFrame({kind: 'workstream-upsert', seq: 3, workstream: validWorkstream}).kind,
  ).toBe('workstream-upsert');
  expect(
    parseCampaignFrame({kind: 'measurement', seq: 4, measurement: validMeasurement}).kind,
  ).toBe('measurement');
  expect(parseCampaignFrame({kind: 'tokens', seq: 5, workstreamId: 'ws-1', tokens: 0}).kind).toBe(
    'tokens',
  );
  expect(parseCampaignFrame({kind: 'status', seq: 6, status: 'completed'}).kind).toBe('status');
});

test('rejects an unknown frame kind and names the path', () => {
  expectInvalid({kind: 'nope', seq: 1}, '$.kind');
});

test('rejects unexpected and missing top-level keys with their paths', () => {
  expectInvalid({kind: 'status', seq: 1, status: 'active', extra: true}, '$.extra');
  expectInvalid({kind: 'status', seq: 1}, '$.status');
});

test('requires every frame sequence to be a positive integer', () => {
  for (const seq of ['1', 0, -1, 1.5, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1]) {
    expectInvalid({kind: 'status', seq, status: 'active'}, '$.seq');
  }
});

test('requires every sequence, order, and token count to be a safe integer', () => {
  const unsafe = Number.MAX_SAFE_INTEGER + 1;
  const cases: Array<{frame: unknown; path: string}> = [
    {
      frame: {
        kind: 'campaign-init',
        seq: 1,
        header: {
          ...validHeader,
          benchmarkVersionBoundary: {
            ...validHeader.benchmarkVersionBoundary,
            afterSequence: unsafe,
          },
        },
      },
      path: '$.header.benchmarkVersionBoundary.afterSequence',
    },
    {
      frame: {
        kind: 'campaign-init',
        seq: 1,
        header: {
          ...validHeader,
          trajectories: [
            {
              ...validTrajectory,
              turns: [{...validTurn, ordinal: unsafe}, ...validTrajectory.turns.slice(1)],
            },
            ...validHeader.trajectories.slice(1),
          ],
        },
      },
      path: '$.header.trajectories[0].turns[0].ordinal',
    },
    {
      frame: {
        kind: 'workstream-upsert',
        seq: 1,
        workstream: {...validWorkstream, firstSequence: unsafe},
      },
      path: '$.workstream.firstSequence',
    },
    {
      frame: {
        kind: 'workstream-upsert',
        seq: 1,
        workstream: {...validWorkstream, lastSequence: unsafe},
      },
      path: '$.workstream.lastSequence',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {...validMeasurement, sequence: unsafe},
      },
      path: '$.measurement.sequence',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {...validMeasurement, sourceOrder: unsafe},
      },
      path: '$.measurement.sourceOrder',
    },
    {
      frame: {kind: 'tokens', seq: 1, workstreamId: 'ws-1', tokens: unsafe},
      path: '$.tokens',
    },
  ];
  for (const {frame, path} of cases) expectInvalid(frame, path);
});

test('accepts real UTC calendar instants and rejects rollovers at every timestamp path', () => {
  const leapBoundary = '2000-02-29T23:59:59.999999Z';
  expect(
    parseCampaignFrame({
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, startedAt: leapBoundary},
    }).kind,
  ).toBe('workstream-upsert');
  expect(
    parseCampaignFrame({
      kind: 'measurement',
      seq: 1,
      measurement: {...validMeasurement, timestamp: leapBoundary},
    }).kind,
  ).toBe('measurement');
  expect(
    parseCampaignFrame({
      kind: 'campaign-init',
      seq: 1,
      header: {
        ...validHeader,
        trajectories: [
          {
            ...validTrajectory,
            turns: [{...validTurn, startedAt: leapBoundary}, ...validTrajectory.turns.slice(1)],
          },
          ...validHeader.trajectories.slice(1),
        ],
      },
    }).kind,
  ).toBe('campaign-init');

  for (const timestamp of [
    '1900-02-29T00:00:00Z',
    '2026-04-31T00:00:00Z',
    '2026-13-01T00:00:00Z',
    '2026-01-01T24:00:00Z',
    '2026-01-01T23:60:00Z',
    '2026-01-01T23:59:60Z',
    '2026-01-01T23:59:59+00:00',
  ]) {
    expectInvalid(
      {
        kind: 'workstream-upsert',
        seq: 1,
        workstream: {...validWorkstream, startedAt: timestamp},
      },
      '$.workstream.startedAt',
    );
  }

  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, finishedAt: '2026-02-29T00:00:00Z'},
    },
    '$.workstream.finishedAt',
  );
  expectInvalid(
    {
      kind: 'measurement',
      seq: 1,
      measurement: {...validMeasurement, timestamp: '2026-02-29T00:00:00Z'},
    },
    '$.measurement.timestamp',
  );
  expectInvalid(
    {
      kind: 'campaign-init',
      seq: 1,
      header: {
        ...validHeader,
        trajectories: [
          {
            ...validTrajectory,
            turns: [
              {...validTurn, startedAt: '2026-02-29T00:00:00Z'},
              ...validTrajectory.turns.slice(1),
            ],
          },
          ...validHeader.trajectories.slice(1),
        ],
      },
    },
    '$.header.trajectories[0].turns[0].startedAt',
  );
});

test('strictly decodes every nested campaign header object', () => {
  const frames: Array<{frame: CampaignFrame | unknown; path: string}> = [
    {
      frame: {kind: 'campaign-init', seq: 1, header: {...validHeader, id: '  '}},
      path: '$.header.id',
    },
    {
      frame: {
        kind: 'campaign-init',
        seq: 1,
        header: {
          ...validHeader,
          objective: {
            ...validHeader.objective,
            metrics: [
              {...validMetric, direction: 'sideways'},
              ...validHeader.objective.metrics.slice(1),
            ],
          },
        },
      },
      path: '$.header.objective.metrics[0].direction',
    },
    {
      frame: {
        kind: 'campaign-init',
        seq: 1,
        header: {
          ...validHeader,
          benchmarkVersions: [
            {
              ...validVersion,
              definitions: [
                {...validDefinition, description: '', unknown: true},
                ...validVersion.definitions.slice(1),
              ],
            },
            ...validHeader.benchmarkVersions.slice(1),
          ],
        },
      },
      path: '$.header.benchmarkVersions[0].definitions[0].unknown',
    },
    {
      frame: {
        kind: 'campaign-init',
        seq: 1,
        header: {
          ...validHeader,
          benchmarkVersionBoundary: {
            ...validHeader.benchmarkVersionBoundary,
            afterSequence: -1,
          },
        },
      },
      path: '$.header.benchmarkVersionBoundary.afterSequence',
    },
    {
      frame: {
        kind: 'campaign-init',
        seq: 1,
        header: {
          ...validHeader,
          trajectories: [
            {
              ...validTrajectory,
              turns: [
                {
                  ...validTurn,
                  startedAt: 'January 1',
                  messages: [{...validMessage, extra: true}],
                },
                ...validTrajectory.turns.slice(1),
              ],
            },
            ...validHeader.trajectories.slice(1),
          ],
        },
      },
      path: '$.header.trajectories[0].turns[0].messages[0].extra',
    },
  ];
  for (const {frame, path} of frames) expectInvalid(frame, path);
});

test('strictly decodes agent identities, fields, and workstream links', () => {
  expectInvalid({kind: 'agent-upsert', seq: 1, agent: {...validAgent, id: ''}}, '$.agent.id');
  expectInvalid({kind: 'agent-upsert', seq: 1, agent: {...validAgent, role: ' '}}, '$.agent.role');
  expectInvalid(
    {kind: 'agent-upsert', seq: 1, agent: {...validAgent, workstreamIds: ['']}},
    '$.agent.workstreamIds[0]',
  );
  expectInvalid(
    {kind: 'agent-upsert', seq: 1, agent: {...validAgent, extra: true}},
    '$.agent.extra',
  );
});

test('strictly decodes measurement scalars and nested values and gates', () => {
  const frames: Array<{frame: unknown; path: string}> = [
    {
      frame: {kind: 'measurement', seq: 1, measurement: {...validMeasurement, id: ''}},
      path: '$.measurement.id',
    },
    {
      frame: {kind: 'measurement', seq: 1, measurement: {...validMeasurement, sequence: 0}},
      path: '$.measurement.sequence',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {...validMeasurement, timestamp: '2026-01-01'},
      },
      path: '$.measurement.timestamp',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {...validMeasurement, measurementKind: 'estimate'},
      },
      path: '$.measurement.measurementKind',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {...validMeasurement, sourceOrder: 1.5},
      },
      path: '$.measurement.sourceOrder',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {
          ...validMeasurement,
          values: [{...validValue, value: Number.NaN}, ...validMeasurement.values.slice(1)],
        },
      },
      path: '$.measurement.values[0].value',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {
          ...validMeasurement,
          gates: [{...validGate, status: 'unknown'}, ...validMeasurement.gates.slice(1)],
        },
      },
      path: '$.measurement.gates[0].status',
    },
    {
      frame: {
        kind: 'measurement',
        seq: 1,
        measurement: {...validMeasurement, surprise: true},
      },
      path: '$.measurement.surprise',
    },
  ];
  for (const {frame, path} of frames) expectInvalid(frame, path);
});

test('validates live workstream scalar domains and populated interval ordering', () => {
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, startedAt: 'not-a-timestamp'},
    },
    '$.workstream.startedAt',
  );
  for (const firstSequence of [0, -1, 1.5]) {
    expectInvalid(
      {kind: 'workstream-upsert', seq: 1, workstream: {...validWorkstream, firstSequence}},
      '$.workstream.firstSequence',
    );
  }
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, finishedAt: 'soon'},
    },
    '$.workstream.finishedAt',
  );
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, lastSequence: 0},
    },
    '$.workstream.lastSequence',
  );
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {
        ...validWorkstream,
        finishedAt: '2025-12-31T23:59:59Z',
        lastSequence: 1,
      },
    },
    '$.workstream.finishedAt',
  );
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, firstSequence: 3, lastSequence: 2},
    },
    '$.workstream.lastSequence',
  );
  expect(
    parseCampaignFrame({
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {
        ...validWorkstream,
        finishedAt: validWorkstream.startedAt,
        lastSequence: validWorkstream.firstSequence,
      },
    }).kind,
  ).toBe('workstream-upsert');
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, phase: 'bogus'},
    },
    '$.workstream.phase',
  );
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, outcome: 'maybe'},
    },
    '$.workstream.outcome',
  );
  expectInvalid(
    {
      kind: 'workstream-upsert',
      seq: 1,
      workstream: {...validWorkstream, surprise: true},
    },
    '$.workstream.surprise',
  );
});

test('requires non-empty token identities and nonnegative integral cumulative tokens', () => {
  for (const workstreamId of ['', ' ', 7]) {
    expectInvalid({kind: 'tokens', seq: 1, workstreamId, tokens: 10}, '$.workstreamId');
  }
  for (const tokens of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, '10']) {
    expectInvalid({kind: 'tokens', seq: 1, workstreamId: 'ws-1', tokens}, '$.tokens');
  }
});

test('rejects an invalid status value', () => {
  expectInvalid({kind: 'status', seq: 1, status: 'paused'}, '$.status');
});

test('rejects a non-object frame', () => {
  expectInvalid(null, '$');
  expectInvalid([], '$');
});

test('isTerminalPhase treats parked as still live, like implementing', () => {
  expect(isTerminalPhase('parked')).toBe(false);
  expect(isTerminalPhase('implementing')).toBe(false);
  expect(isTerminalPhase('evaluated')).toBe(true);
  expect(isTerminalPhase('failed')).toBe(true);
  expect(isTerminalPhase('cancelled')).toBe(true);
});
