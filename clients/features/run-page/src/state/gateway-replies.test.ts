/**
 * The reply attachments the page keeps for each span, as batches of record rows arrive. Input
 * space: 0 to 12 batches of 0 to 8 rows; each row is a reply of one of three spans, an attachment
 * of another role, or a row with no attachment.
 */
import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {BlobAttachedSchema} from '@vibesys/proto';
import type {RecordRow} from '@vibesys/run-state/records';
import fc from 'fast-check';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {gatewayRunMachine} from './index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

type Kind = {span: string; role: string; hash: string} | undefined;

const kinds: fc.Arbitrary<Kind> = fc.option(
  fc.record({
    span: fc.constantFrom('a', 'b', 'c'),
    role: fc.constantFrom('reply', 'reply', 'input'),
    hash: fc.stringMatching(/^[0-9a-f]{4}$/),
  }),
  {nil: undefined},
);
const batches = fc.array(fc.array(kinds, {maxLength: 8}), {maxLength: 12});

/** Rows numbered across the batches, as the records of one run are. */
function rowsOf(input: readonly (readonly Kind[])[]): RecordRow[][] {
  let seq = 0;
  return input.map(batch =>
    batch.map(kind => {
      seq += 1;
      return {
        seq: String(seq),
        epoch: 1,
        body: kind ? 'blob' : 'span',
        event: '',
        message: '',
        failure: false,
        ...(kind
          ? {
              attachment: create(BlobAttachedSchema, {
                span: kind.span,
                role: kind.role,
                blob: {sha256: kind.hash},
              }),
            }
          : {}),
      };
    }),
  );
}

function deepFreeze<T>(value: T): T {
  if (typeof value !== 'object' || value === null || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
}

test('replies are each span’s reply attachments in arrival order, and a batch without one changes nothing', () => {
  fc.assert(
    fc.property(batches, input => {
      const arrivals = rowsOf(input);
      const expected = new Map<string, {seq: bigint; hash: string}[]>();
      let snapshot = getInitialSnapshot(gatewayRunMachine, {seed: SEED});
      for (const rows of arrivals) {
        const before = deepFreeze(snapshot.context.replies);
        snapshot = getNextSnapshot(gatewayRunMachine, snapshot, {type: 'records', rows});
        const replies = rows.filter(row => row.attachment?.role === 'reply');
        for (const row of replies) {
          const list = expected.get(row.attachment?.span ?? '') ?? [];
          list.push({seq: BigInt(row.seq), hash: row.attachment?.blob?.sha256 ?? ''});
          expected.set(row.attachment?.span ?? '', list);
        }
        expect(snapshot.context.replies).toEqual(Object.fromEntries(expected));
        // The map handed on earlier is the one it was: frozen above, so a write would have thrown.
        if (replies.length === 0) expect(snapshot.context.replies).toBe(before);
        else expect(snapshot.context.replies).not.toBe(before);
      }
    }),
    {numRuns: 120, seed: 20261030},
  );
});
