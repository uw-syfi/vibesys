/**
 * What the window and the main process exchange about machines: a `DesktopMachineRequest` and its
 * `DesktopMachineReply` of `proto/vibesys/desktop/v1/settings.proto`, as validated protobuf bytes
 * over one channel. The window sends what the person typed and gets back aliases to offer, the host
 * a save made or found, or the reason it was refused; the main process reads the ssh config and
 * keeps the saved hosts.
 */
import {create, toBinary} from '@bufbuild/protobuf';
import {
  type DesktopMachineReply,
  DesktopMachineReplySchema,
  type DesktopMachineRequest,
  DesktopMachineRequestSchema,
  type DesktopMachineRestore,
  validatedExactFromBinary,
} from '@vibesys/proto';

/** The IPC channel the main process serves; the preload repeats it (a test holds the two equal). */
export const MACHINES_CHANNEL = 'vibesys:machines';

/** What the preload exposes on the window: one request in, one reply out, in exactly one callback. */
export interface MachinesBridge {
  call(
    request: Uint8Array,
    done: (reply: Uint8Array) => void,
    failed: (error: string) => void,
  ): void;
}

/** What a removal took, kept so that putting it back restores it whole. Opaque to the window. */
export type MachineUndo = DesktopMachineRestore;

export type MachineRequest =
  | {readonly op: 'match'; readonly query: string}
  | {readonly op: 'add'; readonly input: string}
  | {readonly op: 'remove'; readonly host: string}
  | {readonly op: 'restore'; readonly undo: MachineUndo};

export type MachineReply =
  | {readonly kind: 'matches'; readonly aliases: readonly string[]}
  | {readonly kind: 'added'; readonly host: string; readonly created: boolean}
  | {readonly kind: 'removed'; readonly undo: MachineUndo}
  | {readonly kind: 'refused'; readonly reason: string};

const requestMessage = (request: MachineRequest): DesktopMachineRequest => {
  switch (request.op) {
    case 'match':
      return create(DesktopMachineRequestSchema, {op: {case: 'match', value: request.query}});
    case 'add':
      return create(DesktopMachineRequestSchema, {op: {case: 'add', value: request.input}});
    case 'remove':
      return create(DesktopMachineRequestSchema, {op: {case: 'remove', value: request.host}});
    case 'restore':
      return create(DesktopMachineRequestSchema, {op: {case: 'restore', value: request.undo}});
  }
};

/** `request` as validated bytes; one the contract refuses is an error. */
export function machineRequestToBytes(request: MachineRequest): Uint8Array {
  const bytes = toBinary(DesktopMachineRequestSchema, requestMessage(request));
  validatedExactFromBinary(DesktopMachineRequestSchema, bytes);
  return bytes;
}

/** The request in `bytes`; bytes that are not a valid message are an error. */
export function machineRequestFromBytes(bytes: Uint8Array): MachineRequest {
  const {op} = validatedExactFromBinary(DesktopMachineRequestSchema, bytes);
  switch (op.case) {
    case 'match':
      return {op: 'match', query: op.value};
    case 'add':
      return {op: 'add', input: op.value};
    case 'remove':
      return {op: 'remove', host: op.value};
    case 'restore':
      return {op: 'restore', undo: op.value};
    case undefined:
      throw new Error('the machine request names no operation');
    default:
      return op satisfies never;
  }
}

const replyMessage = (reply: MachineReply): DesktopMachineReply => {
  switch (reply.kind) {
    case 'matches':
      return create(DesktopMachineReplySchema, {
        result: {case: 'matches', value: {aliases: [...reply.aliases]}},
      });
    case 'added':
      return create(DesktopMachineReplySchema, {
        result: {case: 'added', value: {host: reply.host, created: reply.created}},
      });
    case 'removed':
      return create(DesktopMachineReplySchema, {
        result: {case: 'removed', value: {undo: reply.undo}},
      });
    case 'refused':
      return create(DesktopMachineReplySchema, {
        result: {case: 'refused', value: {reason: reply.reason}},
      });
  }
};

/** `reply` as validated bytes; one the contract refuses is an error. */
export function machineReplyToBytes(reply: MachineReply): Uint8Array {
  const bytes = toBinary(DesktopMachineReplySchema, replyMessage(reply));
  validatedExactFromBinary(DesktopMachineReplySchema, bytes);
  return bytes;
}

/** The reply in `bytes`; bytes that are not a valid message are an error. */
export function machineReplyFromBytes(bytes: Uint8Array): MachineReply {
  const {result} = validatedExactFromBinary(DesktopMachineReplySchema, bytes);
  switch (result.case) {
    case 'matches':
      return {kind: 'matches', aliases: result.value.aliases};
    case 'added':
      return {kind: 'added', host: result.value.host, created: result.value.created};
    case 'removed':
      if (result.value.undo === undefined) throw new Error('the removal carries no undo');
      return {kind: 'removed', undo: result.value.undo};
    case 'refused':
      return {kind: 'refused', reason: result.value.reason};
    case undefined:
      throw new Error('the machine reply names no result');
    default:
      return result satisfies never;
  }
}
