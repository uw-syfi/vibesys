/** Session transport uses the existing HostCall, Tail and GetBlobs paths. */
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {getTextEncoding} from '@bufbuild/protobuf/wire';
import {
  AgentTurnAttrsSchema,
  type ChatMessageRequest,
  ChatMessageRequestSchema,
  type ChatMessageResponse,
  type ChatSessionCreated,
  ChatSessionCreatedSchema,
  type ChatSteerProposed,
  type CreateChatSessionRequest,
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  type HarnessOption,
  HostCallRequestSchema,
  HostCallResponseSchema,
  HostService,
  ListChatSessionsRequestSchema,
  ListChatSessionsResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
  type Record,
  RecordSchema,
  SteerMode,
  SteerTargetKind,
  stringMaxBytes,
  TailRequestSchema,
  TailResponseSchema,
  validatedExactFromBinary,
} from '@vibesys/proto';
import {addressedIntent, IntentRefused, type RunReference} from '../intents/index.js';

/** The most UTF-8 bytes a chat question may hold: the host contract's limit on its text. */
export const CHAT_TEXT_MAX_BYTES: number = stringMaxBytes(ChatMessageRequestSchema.field.text);

export type {
  ChatMessage,
  ChatMessageRequest,
  ChatMessageResponse,
  ChatSteerProposed,
  Record,
} from '@vibesys/proto';
export {
  ChatRole,
  IntentAction,
  type IntentState,
  IntentStep,
  SteerMode,
  SteerTargetKind,
} from '@vibesys/proto';

/** The role of the agent turn a span label describes; null for any other span. */
export function turnRole(
  attrs: {readonly typeUrl: string; readonly value: Uint8Array} | undefined,
): string | null {
  if (attrs?.typeUrl.split('/').at(-1) !== AgentTurnAttrsSchema.typeName) return null;
  return fromBinary(AgentTurnAttrsSchema, attrs.value).role || null;
}

/** The wire enum for a steer's UI-facing mode name; an unnamed mode is the next turn. */
export function steerModeOf(mode: string | undefined): SteerMode {
  return mode === 'standing'
    ? SteerMode.STANDING
    : mode === 'interrupt'
      ? SteerMode.INTERRUPT
      : SteerMode.NEXT_TURN;
}

declare const nonEmpty: unique symbol;
/** A role, attempt or item id; only `targetId` makes one, so an empty id cannot be built. */
export type TargetId = string & {readonly [nonEmpty]: true};

export function targetId(id: string | null | undefined): TargetId {
  if (id === null || id === undefined || id === '') throw new Error('A steer target needs an id');
  return id as TargetId;
}

/**
 * A steer target as the protocol takes it: the whole run names no id, every other kind exactly
 * one (the contract rejects an empty id and an id on the run alike).
 */
export type SteerTargetInit =
  | {readonly kind: SteerTargetKind.RUN}
  | {
      readonly kind: SteerTargetKind.ROLE | SteerTargetKind.ATTEMPT | SteerTargetKind.ITEM;
      readonly id: TargetId;
    };

/** The target the UI's kind name and id mean; an unnamed kind is the whole run. */
export function steerTargetOf(
  kind: string | undefined,
  id: string | null | undefined,
): SteerTargetInit {
  switch (kind) {
    case 'role':
      return {kind: SteerTargetKind.ROLE, id: targetId(id)};
    case 'attempt':
      return {kind: SteerTargetKind.ATTEMPT, id: targetId(id)};
    case 'item':
      return {kind: SteerTargetKind.ITEM, id: targetId(id)};
    default:
      return {kind: SteerTargetKind.RUN};
  }
}

/** What a steer card sends, as the shell receives it. */
export type ChatControl =
  | {readonly action: 'withdraw'; readonly withdraws: string}
  | {
      readonly action: 'steer';
      readonly text: string;
      readonly mode?: string | undefined;
      readonly target?:
        | {readonly kind?: string | undefined; readonly id?: string | null | undefined}
        | undefined;
    };

/** The typed intent a card's control stands for; a malformed target throws before anything is sent. */
export function chatControlIntent(control: ChatControl) {
  if (control.action === 'withdraw')
    return {case: 'withdraw' as const, value: {withdraws: control.withdraws}};
  return {
    case: 'steer' as const,
    value: {
      text: control.text,
      mode: steerModeOf(control.mode),
      target: steerTargetOf(control.target?.kind, control.target?.id),
    },
  };
}
export {HOST_CALL_METHOD, INTENT_TAIL_METHOD as CHAT_TAIL_METHOD} from '../intents/index.js';
export const CHAT_BLOBS_METHOD = `/${HostService.typeName}/${HostService.method.getBlobs.name}`;

export function chatRequest(identity: Omit<ChatMessageRequest, '$typeName'>): ChatMessageRequest {
  return create(ChatMessageRequestSchema, identity);
}
export function hostChatRequest(request: ChatMessageRequest): Uint8Array {
  return toBinary(
    HostCallRequestSchema,
    create(HostCallRequestSchema, {method: {case: 'chatMessage', value: request}}),
  );
}
export function hostChatResponse(
  bytes: Uint8Array,
  request: ChatMessageRequest,
): ChatMessageResponse {
  const result = fromBinary(HostCallResponseSchema, bytes).result;
  if (result.case === 'configurationFailed') throw new IntentRefused(result.value);
  if (
    result.case !== 'chatMessage' ||
    result.value.requestId !== request.requestId ||
    result.value.sessionId !== request.sessionId
  )
    throw new Error('Chat admission identity mismatch');
  return result.value;
}
export function chatTailRequest(runId: string, sessionId: string, fromSeq: bigint): Uint8Array {
  return toBinary(TailRequestSchema, create(TailRequestSchema, {runId, sessionId, fromSeq}));
}
/** Encoded records, as a run's one reader hands them out. */
export function decodeRecords(batch: readonly Uint8Array[]): readonly Record[] {
  return batch.map(bytes => fromBinary(RecordSchema, bytes));
}
export function chatTailResponse(bytes: Uint8Array): readonly Record[] {
  return fromBinary(TailResponseSchema, bytes).records.map(record =>
    fromBinary(RecordSchema, record),
  );
}
export function chatBlobRequest(
  runId: string,
  sessionId: string,
  hashes: readonly string[],
): Uint8Array {
  return toBinary(
    GetBlobsRequestSchema,
    create(GetBlobsRequestSchema, {runId, sessionId, sha256: [...hashes]}),
  );
}
export function chatBlobResponse(bytes: Uint8Array): ReadonlyMap<string, string> {
  const result = new Map<string, string>();
  for (const blob of fromBinary(GetBlobsResponseSchema, bytes).blobs) {
    if (blob.errorCode !== 0) throw new Error(blob.errorMessage);
    result.set(blob.sha256, getTextEncoding().decodeUtf8(blob.data, true));
  }
  return result;
}
export function chatBlobHashes(records: readonly Record[]): readonly string[] {
  return [
    ...new Set(
      records.flatMap(record => {
        const event = record.body.case === 'chat' ? record.body.value.event : null;
        return event?.case === 'message' && event.value.content.case === 'blob'
          ? [event.value.content.value.sha256]
          : [];
      }),
    ),
  ];
}
export function proposalIntent(
  identity: RunReference & {requestId: string; expectedEpoch: bigint},
  proposal: ChatSteerProposed,
) {
  return addressedIntent(identity, identity.requestId, identity.expectedEpoch, {
    case: 'steer',
    value: {text: proposal.text, target: proposal.target, mode: proposal.mode},
  });
}

export function copyChatRequest(request: ChatMessageRequest): ChatMessageRequest {
  return clone(ChatMessageRequestSchema, request);
}

export type {ChatSessionCreated, CreateChatSessionRequest, HarnessOption} from '@vibesys/proto';
export const CHAT_SESSIONS_METHOD = `/${HostService.typeName}/${HostService.method.listChatSessions.name}`;
export const CHAT_CATALOG_METHOD = `/${HostService.typeName}/${HostService.method.read.name}`;

/** The metadata of a session this client just created, before discovery lists it. */
export function createdSession(
  id: string,
  harness: {readonly provider: string; readonly model: string},
): ChatSessionCreated {
  return create(ChatSessionCreatedSchema, {id, provider: harness.provider, model: harness.model});
}
export function sessionListRequest(runId: string): Uint8Array {
  return toBinary(ListChatSessionsRequestSchema, create(ListChatSessionsRequestSchema, {runId}));
}
export function sessionListResponse(bytes: Uint8Array): readonly ChatSessionCreated[] {
  return validatedExactFromBinary(ListChatSessionsResponseSchema, bytes).sessions;
}
export function sessionCreateRequest(
  request: Omit<CreateChatSessionRequest, '$typeName'>,
): Uint8Array {
  return toBinary(
    HostCallRequestSchema,
    create(HostCallRequestSchema, {
      method: {case: 'createChatSession', value: request},
    }),
  );
}
export function sessionCreateResponse(bytes: Uint8Array, sessionId: string): string {
  const result = validatedExactFromBinary(HostCallResponseSchema, bytes).result;
  if (result.case === 'configurationFailed') throw new IntentRefused(result.value);
  if (result.case !== 'createChatSession' || result.value.sessionId !== sessionId)
    throw new Error('Chat creation identity mismatch');
  return sessionId;
}
export function chatCatalogRequest(requestId: string): Uint8Array {
  return toBinary(
    ReadRequestSchema,
    create(ReadRequestSchema, {
      method: {case: 'listHarnesses', value: {requestId, withGenerations: true}},
    }),
  );
}
export function chatCatalogResponse(
  bytes: Uint8Array,
  requestId: string,
): readonly HarnessOption[] {
  const result = validatedExactFromBinary(ReadResponseSchema, bytes).result;
  if (result.case === 'failure') throw new Error(result.value.message);
  if (result.case !== 'listHarnesses' || result.value.requestId !== requestId)
    throw new Error('Chat catalogue identity mismatch');
  return result.value.harnesses;
}
