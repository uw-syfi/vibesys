import {fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {ListEvalPlacesResponseSchema} from '@vibesys/orchestrator-proto';
import {DesktopRefusal} from '@vibesys/proto';
import type {CallTarget} from '../core/transport/gateway-transport.js';
import {MACHINE_CASES, machineCaseHosts, machineCaseRecording} from './machine-cases.js';
import {SUBSCRIBE} from './machine-stats.js';
import {methodPathOf} from './orchestrator/recordings.js';
import type {Recording, Step} from './transport/index.js';

const base: Recording = {respond: () => [{close: true}]};
const target = (host: string, method: string): CallTarget => ({
  host,
  method,
  request: new Uint8Array(),
});

const frames = (steps: readonly Step[]): number => steps.filter(step => 'frame' in step).length;

describe('the Machines worlds of the fixture page', () => {
  it('name every host once, whichever world', () => {
    for (const name of MACHINE_CASES) {
      const keys = machineCaseHosts(name).map(host => host.key);
      expect(new Set(keys).size).toBe(keys.length);
      expect(keys.length).toBeGreaterThan(0);
    }
  });

  it('show several machines of every kind, one of them with a very long alias', () => {
    const hosts = machineCaseHosts('several');
    expect(hosts.length).toBeGreaterThanOrEqual(4);
    expect(Math.max(...hosts.map(host => host.label.length))).toBeGreaterThan(40);
  });

  it('answer the eval places each host has, and no host another’s', () => {
    const recording = machineCaseRecording('several', base);
    const names = (host: string) =>
      recording
        .respond(target(host, methodPathOf('ListEvalPlaces')), 0)
        .flatMap(step =>
          'frame' in step
            ? fromBinary(ListEvalPlacesResponseSchema, step.frame).places.map(place => place.name)
            : [],
        );
    expect(names('ssh:host-a')).toEqual(['host-a', 'cluster-a']);
    expect(names('ssh:host-b')).toEqual(['local']);
    expect(names('local')).not.toEqual(names('ssh:host-a'));
  });

  it('refuse every call of the host the app has no program for', () => {
    const recording = machineCaseRecording('several', base);
    for (const method of [methodPathOf('Hello'), methodPathOf('WatchRuns'), SUBSCRIBE]) {
      expect(recording.respond(target('ssh:host-c', method), 0)).toEqual([
        {
          end: {
            refusal: DesktopRefusal.HOST_PROGRAM_MISSING,
            message: 'no host program for this platform',
          },
        },
      ]);
    }
  });

  it('lets the reconnecting host dial once, send one sample, and then lose its link', () => {
    const recording = machineCaseRecording('several', base);
    const hello = (nth: number) =>
      recording.respond(target('ssh:host-b', methodPathOf('Hello')), nth);
    expect(hello(0)).toEqual([{close: true}]);
    expect(hello(1)).toHaveLength(1);
    expect(hello(1)[0]).toHaveProperty('end');
    const stats = (nth: number) => recording.respond(target('ssh:host-b', SUBSCRIBE), nth);
    expect(frames(stats(0))).toBe(2);
    expect(stats(0).at(-1)).toHaveProperty('end');
    expect(stats(1)).toHaveLength(1);
    expect(frames(stats(1))).toBe(0);
  });

  it('serves a snapshot to a host with stats, and a refusal to one that cannot sample', () => {
    const live = machineCaseRecording('one', base).respond(target('local', SUBSCRIBE), 0);
    expect(frames(live)).toBe(2);
    const unsupported = machineCaseRecording('unsupported', base).respond(
      target('local', SUBSCRIBE),
      0,
    );
    expect(frames(unsupported)).toBe(1);
  });

  it('leaves every other call to the recording it wraps', () => {
    const recording = machineCaseRecording('one', base);
    expect(recording.respond(target('local', methodPathOf('WatchRuns')), 0)).toEqual([
      {close: true},
    ]);
    expect(recording.respond(target('elsewhere', methodPathOf('ListEvalPlaces')), 0)).toEqual([
      {close: true},
    ]);
  });
});
