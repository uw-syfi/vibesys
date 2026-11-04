/** Project a protobuf message the way Python `to_mapping` does.

Enum spellings come from the `adapter_spelling` option. A message whose only
field is `items`, and a message that has a real oneof, unwrap. proto3 optional
absence is null. There is no second spelling table.
*/
import {
  type DescEnum,
  type DescField,
  type DescMessage,
  getOption,
  isFieldSet,
  type Message,
  type MessageShape,
} from '@bufbuild/protobuf';
import {adapter_spelling} from './generated/vibesys/host/v1/operation_options_pb.js';

export function projectMessage(schema: DescMessage, message: Message): unknown {
  const only = schema.fields[0];
  if (schema.fields.length === 1 && only?.name === 'items') return projectField(only, message);
  const oneof = schema.oneofs[0];
  if (oneof !== undefined) {
    const selected = schema.fields.find(
      field => field.oneof === oneof && isFieldSet(message as MessageShape<DescMessage>, field),
    );
    return selected === undefined ? null : projectField(selected, message);
  }
  const result: Record<string, unknown> = {};
  for (const field of schema.fields) {
    const absent =
      field.proto.proto3Optional === true &&
      !isFieldSet(message as MessageShape<DescMessage>, field);
    result[field.name] = absent ? null : projectField(field, message);
  }
  return result;
}

function readField(field: DescField, message: Message): unknown {
  if (field.oneof !== undefined) {
    const group = (message as unknown as Record<string, {case?: string; value?: unknown}>)[
      field.oneof.localName
    ];
    return group?.case === field.localName ? group.value : undefined;
  }
  return (message as unknown as Record<string, unknown>)[field.localName];
}

function projectField(field: DescField, message: Message): unknown {
  const value = readField(field, message);
  switch (field.fieldKind) {
    case 'scalar':
      return projectScalar(value);
    case 'enum':
      return projectEnum(field.enum, value);
    case 'message':
      return value == null ? null : projectMessage(field.message, value as Message);
    case 'list':
      return (value as unknown[] | undefined)?.map(item => projectElement(field, item)) ?? [];
    case 'map':
      return projectMap(field, value);
    default:
      throw new Error('unsupported field');
  }
}

function projectElement(field: DescField & {fieldKind: 'list'}, value: unknown): unknown {
  switch (field.listKind) {
    case 'scalar':
      return projectScalar(value);
    case 'enum':
      return projectEnum(field.enum, value);
    case 'message':
      return projectMessage(field.message, value as Message);
    default:
      throw new Error('unsupported list');
  }
}

function projectMap(
  field: DescField & {fieldKind: 'map'},
  value: unknown,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries((value as Record<string, unknown> | undefined) ?? {})) {
    switch (field.mapKind) {
      case 'scalar':
        result[key] = projectScalar(item);
        break;
      case 'enum':
        result[key] = projectEnum(field.enum, item);
        break;
      case 'message':
        result[key] = projectMessage(field.message, item as Message);
        break;
      default:
        throw new Error('unsupported map');
    }
  }
  return result;
}

function projectScalar(value: unknown): unknown {
  if (typeof value !== 'bigint') return value;
  if (value > BigInt(Number.MAX_SAFE_INTEGER) || value < BigInt(Number.MIN_SAFE_INTEGER)) {
    throw new Error('integer exceeds the adapter range');
  }
  return Number(value);
}

function projectEnum(kind: DescEnum, value: unknown): string {
  const found = kind.values.find(item => item.number === value);
  if (found === undefined || found.number === 0) {
    throw new Error(`${kind.typeName}: missing adapter spelling`);
  }
  const spelling = getOption(found, adapter_spelling);
  if (spelling.length === 0) throw new Error(`${found.name}: missing adapter spelling`);
  return spelling;
}
