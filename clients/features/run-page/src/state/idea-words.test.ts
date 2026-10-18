import {describe, expect, test} from 'bun:test';
import {FIXTURE_NAMES, fixtureNamed} from '@vibesys/run-fixtures';
import {ideaFocusView, ideaOutcomeLine} from './idea-focus.js';
import {EMPTY_TRACE_LOG, traceStep} from './live.js';
import {createRunReader, type RunModel} from './run-model.js';

/**
 * What an idea says about its result, over every fixture run: the open idea's line, its badge and
 * the hero's selected-idea line agree and each fact is worded once (issue #3027's vocabulary).
 */
const MODELS: readonly (readonly [string, RunModel])[] = FIXTURE_NAMES.map(name => [
  name,
  createRunReader().read(
    traceStep(EMPTY_TRACE_LOG, {kind: 'events', events: fixtureNamed(name).events}),
    null,
  ),
]);

const ideas = (outcome: 'passed' | 'failed') =>
  MODELS.flatMap(([name, model]) =>
    model.candidates.candidates
      .filter(item => item.outcome === outcome)
      .map(item => ({name, model, item})),
  );

describe('a failed idea', () => {
  test('never shows its multiple as a bare result: the line says why it failed or says it was measured', () => {
    let measured = 0;
    for (const {name, model, item} of ideas('failed')) {
      const line = ideaOutcomeLine(model, item);
      expect({name, line}).not.toEqual({name, line: expect.stringMatching(/^Failed · \d/)});
      if (item.measurement?.vsBaseline != null && item.measurement.metricValue !== null) {
        measured += 1;
        expect(line).toMatch(/ · measured \d[\d.,]*× the input/);
        expect(line.split(' · ')[0]).toMatch(/^[A-Z]/);
        expect(line.split(' · ')[0]).not.toMatch(/\d×/);
      }
    }
    expect(measured).toBeGreaterThan(0);
  });

  test('leads with its reason, not with the status its badge already says', () => {
    for (const {model, item} of ideas('failed')) {
      expect(ideaOutcomeLine(model, item)).not.toMatch(/^Failed · /);
    }
  });

  test('has no multiple on the hero’s selected-idea line, and a capitalized status', () => {
    for (const {model, item} of ideas('failed')) {
      const view = ideaFocusView(model, item.id);
      expect(view?.ratio).toBeNull();
      expect(view?.outcome).toMatch(/^Failed/);
    }
  });
});

describe('a passed idea', () => {
  test('is Passed in its line, and the leader says Adopted or Best before it', () => {
    let leaders = 0;
    for (const {model, item} of ideas('passed')) {
      const line = ideaOutcomeLine(model, item);
      const best = model.candidates.best;
      if (best?.candidate === item.id) {
        leaders += 1;
        expect(line).toMatch(best.adopted ? /^Adopted · passed/ : /^Best · passed/);
      } else {
        expect(line).toMatch(/^Passed/);
      }
    }
    expect(leaders).toBeGreaterThan(0);
  });
});
