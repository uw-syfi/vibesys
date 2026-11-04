import {expect, it} from '@effect/vitest';
import * as Schema from 'effect/Schema';
import {type FaultPlan, faultySteps, type Step, withFaults} from './index.js';

const frames = (count: number): Step[] =>
  Array.from({length: count}, (_, i) => ({frame: Uint8Array.of(i)}));
const bytesOf = (steps: ReadonlyArray<Step>): number[] =>
  steps.flatMap(step => ('frame' in step ? [step.frame[0] ?? -1] : []));

const small = Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 8}));

it.prop(
  'faults only repeat or cut frames: what is delivered is a prefix of the original with repeats',
  {count: small, drop: small, dup: Schema.Array(small), call: small},
  ({count, drop, dup, call}) => {
    const out = faultySteps(frames(count), {dropAfter: [drop], duplicate: dup}, call);
    const delivered = bytesOf(out);
    const collapsed = delivered.filter((value, i) => value !== delivered[i - 1]);
    expect(collapsed).toEqual(Array.from({length: collapsed.length}, (_, i) => i));
    if (call !== 0) expect(delivered.length).toBeGreaterThanOrEqual(count);
  },
);

it('a dropped call ends with the link lost after the frames before it', () => {
  const out = faultySteps(frames(5), {dropAfter: [2], duplicate: []}, 0);
  expect(bytesOf(out)).toEqual([0, 1]);
  expect(out.at(-1)).toEqual({end: {message: 'connection lost', status: 14}});
});

it('a stalled call hangs after its frames', () => {
  const out = faultySteps(frames(5), {dropAfter: [], duplicate: [], stallAt: 3}, 0);
  expect(bytesOf(out)).toEqual([0, 1, 2]);
  expect(out.at(-1)).toEqual({hang: true});
});

it('a recording with a dropped link ends its call with status 14 after the frames before it', () => {
  const plan: FaultPlan = {dropAfter: [1], duplicate: [0]};
  const recording = withFaults({respond: () => frames(3)}, plan);
  const target = {host: 'local', method: '/p.S/M', request: new Uint8Array()};
  expect(bytesOf(recording.respond(target, 0))).toEqual([0, 0]);
  expect(recording.respond(target, 0).at(-1)).toEqual({
    end: {message: 'connection lost', status: 14},
  });
  expect(bytesOf(recording.respond(target, 1))).toEqual([0, 0, 1, 2]);
});
