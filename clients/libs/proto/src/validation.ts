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
