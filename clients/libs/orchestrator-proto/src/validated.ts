/** Decode and encode at the process boundary: bytes become a message only if its rules hold. */
import {type DescMessage, fromBinary, type MessageShape, toBinary} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';

const validator = createValidator();

/**
 * Decode `bytes` as `schema` and check its protovalidate rules. Throws an `Error` that names the
 * message and the offending field path, so a bad frame is reported rather than half-applied.
 */
export function decodeValidated<D extends DescMessage>(
  schema: D,
  bytes: Uint8Array,
): MessageShape<D> {
  const message = fromBinary(schema, bytes);
  const result = validator.validate(schema, message);
  if (result.kind === 'valid') return message;
  const detail = result.violations?.map(violation => violation.toString()).join('; ');
  throw new Error(`invalid ${schema.typeName}: ${detail ?? String(result.error)}`);
}

/** Encode a message for the wire. */
export function encodeMessage<D extends DescMessage>(
  schema: D,
  message: MessageShape<D>,
): Uint8Array {
  return toBinary(schema, message);
}
