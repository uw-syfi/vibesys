import {expect, test} from 'bun:test';
import type {FrameOperatorAction} from './campaign-frames.js';
import {dwellMessage, dwellTime, formatDwell, steeredPromptTail} from './operator-timeline.js';

function entry(overrides: Partial<FrameOperatorAction> = {}): FrameOperatorAction {
  return {
    action: 'pause_requested',
    text: '',
    agentKind: null,
    roundLabel: null,
    executionId: null,
    clientId: null,
    timestamp: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

test('dwellTime is null when there have been no operator actions', () => {
  expect(dwellTime([], Date.parse('2026-01-01T00:00:00Z'))).toBeNull();
});

test('dwellTime is null once a pause has been resumed', () => {
  const entries = [
    entry({action: 'pause_requested', timestamp: '2026-01-01T00:00:00Z'}),
    entry({action: 'paused', timestamp: '2026-01-01T00:00:05Z'}),
    entry({action: 'resumed', timestamp: '2026-01-01T00:05:00Z'}),
  ];
  expect(dwellTime(entries, Date.parse('2026-01-01T01:00:00Z'))).toBeNull();
});

test('dwellTime reports an unresolved pause, elapsed since it landed', () => {
  const entries = [
    entry({action: 'pause_requested', timestamp: '2026-01-01T00:00:00Z'}),
    entry({action: 'paused', timestamp: '2026-01-01T00:00:05Z'}),
  ];
  const now = Date.parse('2026-01-01T00:12:05Z');
  const dwell = dwellTime(entries, now);
  expect(dwell).not.toBeNull();
  expect(dwell?.action).toBe('paused');
  expect(dwell?.since).toBe('2026-01-01T00:00:05Z');
  expect(dwell?.elapsedMs).toBe(12 * 60_000);
  expect(dwell?.severity).toBe('normal');
});

test('dwellTime reports an unresolved stop request as waiting, not resolved', () => {
  const entries = [entry({action: 'stop_requested', timestamp: '2026-01-01T00:00:00Z'})];
  const dwell = dwellTime(entries, Date.parse('2026-01-01T00:10:00Z'));
  expect(dwell?.action).toBe('stop_requested');
});

test('dwellTime is null once a run has actually stopped', () => {
  const entries = [
    entry({action: 'stop_requested', timestamp: '2026-01-01T00:00:00Z'}),
    entry({action: 'stopped', timestamp: '2026-01-01T00:00:05Z'}),
  ];
  expect(dwellTime(entries, Date.parse('2026-01-01T01:00:00Z'))).toBeNull();
});

test('dwellTime escalates past the one-hour threshold', () => {
  const entries = [entry({action: 'paused', timestamp: '2026-01-01T00:00:00Z'})];
  const justUnder = dwellTime(entries, Date.parse('2026-01-01T00:59:59Z'));
  const atThreshold = dwellTime(entries, Date.parse('2026-01-01T01:00:00Z'));
  expect(justUnder?.severity).toBe('normal');
  expect(atThreshold?.severity).toBe('escalated');
});

test('dwellTime ignores steer entries: steering does not start or end a wait', () => {
  const entries = [
    entry({action: 'steer_queued', text: 'focus on latency', timestamp: '2026-01-01T00:00:00Z'}),
    entry({action: 'steer_consumed', timestamp: '2026-01-01T00:00:05Z'}),
  ];
  expect(dwellTime(entries, Date.parse('2026-01-01T01:00:00Z'))).toBeNull();
});

test('dwellTime tracks only the most recent wait, not an earlier resolved one', () => {
  const entries = [
    entry({action: 'pause_requested', timestamp: '2026-01-01T00:00:00Z'}),
    entry({action: 'resumed', timestamp: '2026-01-01T00:05:00Z'}),
    entry({action: 'stop_requested', timestamp: '2026-01-01T00:10:00Z'}),
  ];
  const dwell = dwellTime(entries, Date.parse('2026-01-01T00:20:00Z'));
  expect(dwell?.action).toBe('stop_requested');
  expect(dwell?.since).toBe('2026-01-01T00:10:00Z');
});

test('formatDwell renders hours and minutes, or minutes alone under an hour', () => {
  expect(formatDwell(45 * 60_000)).toBe('45m');
  expect(formatDwell(6 * 60 * 60_000 + 12 * 60_000)).toBe('6h 12m');
  expect(formatDwell(0)).toBe('0m');
});

test('dwellMessage names the waiting action and reads as a sentence', () => {
  const dwell = dwellTime(
    [entry({action: 'paused', timestamp: '2026-01-01T00:00:00Z'})],
    Date.parse('2026-01-01T06:12:00Z'),
  );
  if (dwell === null) throw new Error('expected a dwell');
  expect(dwellMessage(dwell)).toBe('Paused 6h 12m, waiting on an operator');
});

test('steeredPromptTail joins a steer_consumed entry to the execution it landed on', () => {
  const consumed = entry({action: 'steer_consumed', executionId: 'inv-7'});
  const executions = [
    {executionId: 'inv-6', userPrompt: 'earlier prompt'},
    {executionId: 'inv-7', userPrompt: 'Focus on reducing tail latency.'},
  ];
  expect(steeredPromptTail(consumed, executions)).toBe('Focus on reducing tail latency.');
});

test('steeredPromptTail is null when no execution matches yet', () => {
  const consumed = entry({action: 'steer_consumed', executionId: 'inv-7'});
  expect(steeredPromptTail(consumed, [])).toBeNull();
});

test('steeredPromptTail is null when the entry carries no executionId', () => {
  const consumed = entry({action: 'steer_consumed', executionId: null});
  expect(steeredPromptTail(consumed, [{executionId: 'inv-7', userPrompt: 'x'}])).toBeNull();
});

test('steeredPromptTail is null for an entry that is not a steer_consumed', () => {
  const requested = entry({action: 'steer_queued', executionId: 'inv-7'});
  expect(steeredPromptTail(requested, [{executionId: 'inv-7', userPrompt: 'x'}])).toBeNull();
});
