/** The generated schema is the sole source of field validation rules. */
import {
  type DescField,
  type DescMessage,
  fromBinary,
  getOption,
  isFieldSet,
  type MessageShape,
} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';
import {field as fieldRules, StringRulesSchema} from './generated/buf/validate/validate_pb.js';
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

/** The `max_bytes` a string field's buf.validate rule sets; throws naming the field if none. */
export function stringMaxBytes(field: DescField): number {
  const rules = getOption(field, fieldRules);
  if (
    rules.type.case !== 'string' ||
    !isFieldSet(rules.type.value, StringRulesSchema.field.maxBytes)
  )
    throw new Error(`${field.parent.typeName}.${field.name}: no string max_bytes rule`);
  return Number(rules.type.value.maxBytes);
}
