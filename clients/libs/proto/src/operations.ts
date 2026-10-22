/** Opaque accepted requests own immutable bytes; builders never enter a transport. */
import {
  create,
  type DescMessage,
  fromBinary,
  isMessage,
  type MessageInitShape,
  type MessageShape,
  toBinary,
} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';

const validator = createValidator();
const maxBytes = 1 << 20;
const acceptedBrand: unique symbol = Symbol('accepted operation');
export interface Accepted<Name extends string> {
  readonly [acceptedBrand]: Name;
}
export interface OperationPolicy {
  readonly name: string;
  readonly id: number;
  readonly capMs: number;
  readonly graceMs: number;
  readonly totalMs: number;
}
/** Structural cancellation contract keeps this foundation independent of DOM/Node types. */
export interface OperationSignal {
  readonly aborted: boolean;
  readonly reason?: unknown;
  addEventListener(type: 'abort', listener: () => void, options?: {once?: boolean}): void;
  removeEventListener(type: 'abort', listener: () => void): void;
}
function checkCancellation(signal: OperationSignal): void {
  if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
}

export interface OperationTransport {
  /** Dial and queue time consume totalMs; the shell owns cancellation. */
  invoke(
    id: number,
    payload: Uint8Array,
    totalMs: number,
    signal: OperationSignal,
  ): Promise<Uint8Array>;
}
const payloads = new WeakMap<object, Uint8Array>();

function checked<Schema extends DescMessage>(
  schema: Schema,
  message: MessageShape<Schema>,
): Uint8Array {
  const result = validator.validate(schema, message);
  if (result.kind !== 'valid')
    throw new Error(result.kind === 'invalid' ? result.error.message : 'invalid operation schema');
  const payload = toBinary(schema, message);
  if (payload.byteLength !== toBinary(schema, message, {writeUnknownFields: false}).byteLength) {
    throw new Error('operation contains unknown fields');
  }
  if (payload.byteLength > maxBytes) throw new Error('operation payload exceeds 1 MiB');
  return payload;
}

export function accept<const Policy extends OperationPolicy, Schema extends DescMessage>(
  policy: Policy,
  schema: Schema,
  input: MessageInitShape<Schema>,
): Accepted<Policy['name']> {
  if (isMessage(input, schema)) checked(schema, input);
  const payload = checked(schema, create(schema, input));
  const accepted = Object.freeze({[acceptedBrand]: policy.name});
  payloads.set(accepted, payload.slice());
  return accepted;
}

function field(value: unknown, path: readonly string[]): unknown {
  for (const name of path) {
    if (typeof value !== 'object' || value === null) throw new Error('invalid correlation path');
    value = (value as Record<string, unknown>)[name];
  }
  return value;
}

function sameValue(left: unknown, right: unknown): boolean {
  if (left instanceof Uint8Array && right instanceof Uint8Array) {
    return left.length === right.length && left.every((value, index) => value === right[index]);
  }
  return left === right;
}

export interface CallDefinition<
  Policy extends OperationPolicy,
  Request extends DescMessage,
  Response extends DescMessage,
> {
  readonly policy: Policy;
  readonly requestSchema: Request;
  readonly responseSchema: Response;
  readonly correlations: readonly (readonly [readonly string[], readonly string[]])[];
}

export async function call<
  Policy extends OperationPolicy,
  Request extends DescMessage,
  Response extends DescMessage,
>(
  transport: OperationTransport,
  definition: CallDefinition<Policy, Request, Response>,
  request: Accepted<Policy['name']>,
  signal: OperationSignal,
): Promise<MessageShape<Response>> {
  const {policy, requestSchema, responseSchema, correlations} = definition;
  checkCancellation(signal);
  const bytes = payloads.get(request);
  if (!bytes || request[acceptedBrand] !== policy.name)
    throw new Error('unaccepted operation request');
  const input = fromBinary(requestSchema, bytes);
  checked(requestSchema, input);
  const payload = await transport.invoke(policy.id, bytes.slice(), policy.totalMs, signal);
  checkCancellation(signal);
  if (payload.byteLength > maxBytes) throw new Error('operation payload exceeds 1 MiB');
  const response = fromBinary(responseSchema, payload);
  checked(responseSchema, response);
  for (const [left, right] of correlations) {
    if (!sameValue(field(input, left), field(response, right)))
      throw new Error('response correlation mismatch');
  }
  return response;
}
