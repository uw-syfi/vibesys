import {describe, expect, test} from 'bun:test';
import {RUN_EVENT_TYPES_BY_KINDS} from '@vibesys/contracts/host-helper';
import {isTraceEventType, SPAN_EVENTS} from './live.js';

/**
 * The events the trace slice keeps and the events a host selects for the `trace` kind of a history
 * read are one set: the host defines it (`server.host_run_events`, exported in the generated
 * contract), and a drift between the two would leave the scrubber short of positions on a host that
 * selects, or make the page read events it then drops.
 */
describe('the events the trace slice keeps', () => {
  test('are the events the host selects for the trace kind', () => {
    expect([...SPAN_EVENTS].sort()).toEqual([...RUN_EVENT_TYPES_BY_KINDS.trace].sort());
  });

  test('are recognised by type, and no other type is', () => {
    for (const type of RUN_EVENT_TYPES_BY_KINDS.trace) expect(isTraceEventType(type)).toBe(true);
    for (const type of ['agent_output_chunk', 'span', 'SPAN_STARTED', '', 7, null, undefined]) {
      expect(isTraceEventType(type)).toBe(false);
    }
  });
});
