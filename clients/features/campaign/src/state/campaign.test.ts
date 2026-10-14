import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {parseArchitectureGraph} from '@vibesys/contracts';
import {checkMachine} from '@vibesys/machine/testing';
import {createActor} from 'xstate';
import {campaignMachine, parseReplayScenario, projectCampaign} from './index.js';

const record = parseReplayScenario(
  JSON.parse(
    readFileSync(
      resolve(import.meta.dir, '../../../../apps/web/dev/fixtures/trajectory-replay.json'),
      'utf8',
    ),
  ),
);
const architecture = parseArchitectureGraph(
  JSON.parse(readFileSync(resolve(import.meta.dir, '../fixtures/architecture-queue.json'), 'utf8')),
);

test('model: playback owns one delay, scrubbing stays bounded, and close releases every operation', () => {
  checkMachine(
    {
      machine: campaignMachine,
      input: {mode: 'history', record},
      event: (random, live, snapshot) =>
        snapshot.status === 'done'
          ? null
          : random.pick([
              {type: 'point', index: random.int(record.measurements.length + 20) - 10},
              {type: 'play', playing: random.bool()},
              {type: 'metric', id: random.pick(record.objective.metrics).id},
              {type: 'workstream', id: random.pick(record.workstreams).id},
              {type: 'close'},
              ...live.delays.map(delay => live.fire(delay)),
            ]),
      invariants: snapshot => {
        const view = projectCampaign(snapshot.context).campaign;
        if (view === null) return ['history has no record'];
        const failures: string[] = [];
        if (view.pointIndex < 0 || view.pointIndex >= record.measurements.length)
          failures.push('cursor escaped measurement range');
        if (snapshot.status !== 'done' && view.playing !== snapshot.matches({history: 'playing'}))
          failures.push('playback mode disagrees with view');
        return failures;
      },
      also: (before, event, after) => {
        if (event.type === 'workstream')
          expect(projectCampaign(after.context).campaign?.pointIndex).toBe(
            projectCampaign(before.context).campaign?.pointIndex,
          );
      },
    },
    80,
  );
});

test('model: loading retries through typed outcomes and never keeps an operation after closing', () => {
  checkMachine({
    machine: campaignMachine,
    input: {mode: 'replay'},
    event: (random, live, snapshot) =>
      snapshot.status === 'done'
        ? null
        : random.pick([
            {type: 'load'},
            {type: 'close'},
            ...live.operations.flatMap(operation => [
              live.done(operation, record),
              live.error(operation, new Error('fixture unavailable')),
            ]),
          ]),
    invariants: snapshot =>
      snapshot.status === 'done' ||
      snapshot.context.loading === snapshot.matches('loading') ||
      snapshot.matches('idle')
        ? []
        : ['loading does not match operation'],
  });
});

test('component detail selection is machine-owned and survives measurement scrubbing', () => {
  const actor = createActor(campaignMachine, {
    input: {mode: 'history', record: {...record, architecture}},
  });
  actor.start();

  actor.send({type: 'architecture-component', id: 'queue.producer'});
  actor.send({type: 'point', index: 0});
  expect(
    projectCampaign(actor.getSnapshot().context).campaign?.selectedArchitectureComponent?.id,
  ).toBe('queue.producer');

  actor.send({type: 'architecture-component', id: null});
  expect(
    projectCampaign(actor.getSnapshot().context).campaign?.selectedArchitectureComponent,
  ).toBeNull();
  actor.stop();
});
