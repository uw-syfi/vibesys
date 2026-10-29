/** The generated schema is the sole source of field validation rules. */
import {type DescMessage, fromBinary, type MessageShape} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';
import {HostService} from './generated/vibesys/api/v1/host_pb.js';

const validator = createValidator();

/** Decode and validate at a process boundary; failures include the offending field path. */
export function validatedFromBinary<D extends DescMessage>(
  schema: D,
  bytes: Uint8Array,
): MessageShape<D> {
  const message = fromBinary(schema, bytes);
  const result = validator.validate(schema, message);
  if (result.kind !== 'valid') throw result.error;
  return message;
}

/** Validate a generic bridge payload using the service's generated method descriptors. */
export function validateHostPayload(
  method: string,
  direction: 'input' | 'output',
  bytes: Uint8Array,
): void {
  const descriptor = HostService.methods.find(
    value => `/${HostService.typeName}/${value.name}` === method,
  );
  if (!descriptor) throw new Error(`unknown HostService method: ${method}`);
  validatedFromBinary(descriptor[direction], bytes);
}

/** Config and routing inputs are closed contracts; retained data readers use validatedFromBinary. */
export function validatedExactFromBinary<D extends DescMessage>(
  schema: D,
  bytes: Uint8Array,
): MessageShape<D> {
  const message = validatedFromBinary(schema, bytes);
  rejectUnknownFields(message, schema.typeName);
  return message;
}

function rejectUnknownFields(value: unknown, path: string): void {
  if (value === null || typeof value !== 'object' || value instanceof Uint8Array) return;
  const record = value as Record<string, unknown>;
  const unknown = record['$unknown'];
  if (Array.isArray(unknown) && unknown.length > 0) {
    const tag = (unknown[0] as {no: number}).no;
    throw new Error(`${path}.#${tag}: unknown protobuf field`);
  }
  for (const [key, nested] of Object.entries(record)) {
    if (key !== '$typeName' && key !== '$unknown') rejectUnknownFields(nested, `${path}.${key}`);
  }
}
