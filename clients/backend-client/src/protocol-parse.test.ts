import {describe, expect, it} from 'bun:test';
import {BackendClientError} from './errors.js';
import type {ServerMessage} from './protocol.js';
import {parseServerMessage} from './protocol-parse.js';

/** The rejection `action` threw, so a test can assert on its kind and message. */
function rejection(action: () => unknown): BackendClientError {
  try {
    action();
  } catch (error) {
    if (error instanceof BackendClientError) return error;
    throw error;
  }
  throw new Error('expected a rejection');
}

function line(message: unknown): string {
  return JSON.stringify(message);
}

describe('parseServerMessage history_after_sequence', () => {
  /**
   * The list is every shape the wire can actually carry. `NaN` and `Infinity`
   * are not in it because JSON cannot represent either, and this boundary's
   * input is a JSON line, so no server can send them.
   */
  it('rejects a floor that cannot be a sequence, naming the key', () => {
    for (const floor of [-1, -4_096, 12.5, '0', null, true]) {
      const error = rejection(() =>
        parseServerMessage(line({type: 'event_batch', events: [], history_after_sequence: floor})),
      );
      expect(error.kind).toBe('parse');
      expect(error.message).toContain('history_after_sequence');
      expect(error.message).toContain(String(floor));
    }
  });

  it('accepts an omitted, zero, and positive floor', () => {
    const messages: readonly ServerMessage[] = [
      {type: 'event_batch', events: []},
      {type: 'event_batch', events: [], history_after_sequence: 0},
      {
        type: 'event_batch',
        events: [{type: 'run_started', timestamp: '2026-01-01T00:00:00Z'}],
        history_after_sequence: 4_096,
      },
    ];
    for (const message of messages) {
      expect(parseServerMessage(line(message))).toEqual(message);
    }
  });

  it('leaves the other message types alone', () => {
    // Only `event_batch` carries the field, so the check must not reach a
    // message that happens to have something else under that key.
    const subscribed = line({
      type: 'subscribed',
      request_id: 'r1',
      run_id: 'run',
      latest_sequence: 7,
      history_after_sequence: -1,
    });
    expect(parseServerMessage(subscribed).type).toBe('subscribed');
  });
});
