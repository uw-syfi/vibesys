import {describe, expect, test} from 'bun:test';
import {RunBinding} from './run-binding.js';

function harness() {
  const detached: string[] = [];
  const binding = new RunBinding({detached: runId => detached.push(runId)});
  return {binding, detached};
}

describe('RunBinding', () => {
  test('the first rebind takes the run without reporting anything detached', () => {
    const {binding, detached} = harness();
    binding.rebind('run-a');
    expect(binding.current).toBe('run-a');
    expect(detached).toEqual([]);
  });

  test('rebinding a second run takes it immediately and reports the first detached', () => {
    const {binding, detached} = harness();
    binding.rebind('run-a');
    binding.rebind('run-b');
    expect(binding.current).toBe('run-b');
    expect(detached).toEqual(['run-a']);
  });

  test('rebind takes the new run before reporting the old one detached', () => {
    // The regression this guards: a `detached` effect that reads `current` back (as a reentrant
    // release through whatever the caller's own detach handling triggers) must see the new run,
    // not the run being replaced, or the caller's reentrant cleanup acts on stale state.
    const seenDuringDetach: Array<string | null> = [];
    const binding: RunBinding = new RunBinding({
      detached: () => seenDuringDetach.push(binding.current),
    });
    binding.rebind('run-a');
    binding.rebind('run-b');
    expect(seenDuringDetach).toEqual(['run-b']);
  });

  test('release drops the current run without reporting it detached', () => {
    const {binding, detached} = harness();
    binding.rebind('run-a');
    binding.release('run-a');
    expect(binding.current).toBeNull();
    expect(detached).toEqual([]);
  });

  test('release of a stale run id leaves the current run bound', () => {
    const {binding, detached} = harness();
    binding.rebind('run-a');
    binding.rebind('run-b');
    detached.length = 0;
    binding.release('run-a');
    expect(binding.current).toBe('run-b');
    expect(detached).toEqual([]);
  });

  test('detachCurrent reports and clears whatever is bound', () => {
    const {binding, detached} = harness();
    binding.rebind('run-a');
    binding.detachCurrent();
    expect(binding.current).toBeNull();
    expect(detached).toEqual(['run-a']);
  });

  test('detachCurrent is safe when nothing is bound', () => {
    const {binding, detached} = harness();
    binding.detachCurrent();
    expect(binding.current).toBeNull();
    expect(detached).toEqual([]);
  });
});
