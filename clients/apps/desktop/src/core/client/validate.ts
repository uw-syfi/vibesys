/** Messages cross the gateway only if their protovalidate rules hold, in both directions. */
import {
  create,
  type DescMessage,
  fromBinary,
  type MessageInitShape,
  type MessageShape,
  toBinary,
} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';
import * as Effect from 'effect/Effect';
import {InvalidMessage} from '../errors.js';

const validator = createValidator();

const invalid = (
  direction: 'request' | 'response',
  schema: DescMessage,
  detail: string,
): InvalidMessage => new InvalidMessage({direction, type: schema.typeName, detail});

const describe = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

/** Check `message` against its rules; the detail names each violated field. */
const violationsOf = (
  schema: DescMessage,
  message: MessageShape<DescMessage>,
): string | undefined => {
  const result = validator.validate(schema, message);
  if (result.kind === 'valid') return undefined;
  if (result.kind === 'error') return describe(result.error);
  return result.violations.map(violation => violation.toString()).join('; ');
};

/** Build the request from its initializer and encode it, or fail without producing bytes. */
export const encodeRequest = <D extends DescMessage>(
  schema: D,
  init: MessageInitShape<D>,
): Effect.Effect<Uint8Array, InvalidMessage> =>
  Effect.try({
    try: () => {
      const message = create(schema, init);
      const detail = violationsOf(schema, message);
      if (detail !== undefined) throw new Error(detail);
      return toBinary(schema, message);
    },
    catch: cause => invalid('request', schema, describe(cause)),
  });

/** Decode a reply frame and check it, or fail: a frame is either whole and valid or not emitted. */
export const decodeResponse = <D extends DescMessage>(
  schema: D,
  bytes: Uint8Array,
): Effect.Effect<MessageShape<D>, InvalidMessage> =>
  Effect.try({
    try: () => {
      const message = fromBinary(schema, bytes);
      const detail = violationsOf(schema, message);
      if (detail !== undefined) throw new Error(detail);
      return message;
    },
    catch: cause => invalid('response', schema, describe(cause)),
  });
