/** Pure decoding and projection of the run log's native transcript stream. */
import {fromBinary, type MessageShape} from '@bufbuild/protobuf';
import {timestampMs} from '@bufbuild/protobuf/wkt';
import {type ToolResultPayload, validateToolResultPayload} from '@vibesys/contracts';
import {
  RUN_LOG_MAX_RECORD_BYTES,
  TranscriptChannel,
  type TranscriptContent,
  TranscriptEntrySchema,
} from '@vibesys/proto';
import {ownProjectionInput} from '../publication.js';
import type {TranscriptEntry} from '../transcript-fold.js';

type NativeEntry = MessageShape<typeof TranscriptEntrySchema>;
export interface NativeTranscript {
  readonly pending: Uint8Array;
  readonly offset: number;
  readonly entries: readonly {readonly offset: number; readonly value: NativeEntry}[];
}
export function emptyNativeTranscript(): NativeTranscript {
  return {pending: new Uint8Array(), offset: 0, entries: []};
}

function frameLength(bytes: Uint8Array, start: number): {size: number; offset: number} | undefined {
  let size = 0;
  let offset = start;
  for (let shift = 0; shift < 35 && offset < bytes.length; shift += 7) {
    const byte = bytes[offset++] ?? 0;
    size += (byte & 127) * 2 ** shift;
    if (size > RUN_LOG_MAX_RECORD_BYTES) throw new Error('Transcript frame exceeds record limit');
    if (byte < 128) return {size, offset};
    if (shift === 28) throw new Error('Invalid transcript frame length');
  }
  return undefined;
}

/** A partial varint or payload stays pending; malformed or oversized frames fail by name. */
export function appendNativeTranscript(
  state: NativeTranscript,
  chunk: Uint8Array,
): NativeTranscript {
  const bytes = new Uint8Array([...state.pending, ...chunk]);
  const entries = [...state.entries];
  let cursor = 0;
  while (cursor < bytes.length) {
    const start = cursor;
    const frame = frameLength(bytes, start);
    if (!frame || frame.offset + frame.size > bytes.length) break;
    const {size} = frame;
    cursor = frame.offset;
    if (size === 0) throw new Error('Empty transcript frame');
    const value = fromBinary(TranscriptEntrySchema, bytes.slice(cursor, cursor + size));
    if (!value.entry.case) throw new Error('Transcript frame has no entry');
    entries.push({offset: state.offset + start, value});
    cursor += size;
  }
  return {pending: bytes.slice(cursor), offset: state.offset + cursor, entries};
}

function contents(entry: NativeEntry): readonly (TranscriptContent | undefined)[] {
  switch (entry.entry.case) {
    case 'text':
      return [entry.entry.value.content];
    case 'toolCall':
      return [entry.entry.value.argumentsJson];
    case 'toolResult':
      return [entry.entry.value.content, entry.entry.value.payloadJson];
    default:
      throw new Error('Transcript frame has no entry');
  }
}
export function nativeTranscriptHashes(transcript: NativeTranscript): readonly string[] {
  return transcript.entries.flatMap(({value}) =>
    contents(value).flatMap(content =>
      content?.value.case === 'blob' ? [content.value.value.sha256] : [],
    ),
  );
}
function text(
  content: TranscriptContent | undefined,
  blobs: Readonly<Record<string, string>>,
): string | undefined {
  if (!content) throw new Error('Transcript entry has no content');
  switch (content.value.case) {
    case 'inline':
      return content.value.value;
    case 'blob':
      return blobs[content.value.value.sha256];
    default:
      throw new Error('Transcript content has no representation');
  }
}
function argumentsOf(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))
    throw new Error('Transcript tool arguments must be an object');
  return parsed as Record<string, unknown>;
}

type Base = Pick<TranscriptEntry, 'id' | 'spanId' | 'turnId' | 'invocationId' | 'at'>;
type EntryBody = NativeEntry['entry'];
type Blobs = Readonly<Record<string, string>>;
function textEntry(
  base: Base,
  entry: Extract<EntryBody, {case: 'text'}>['value'],
  blobs: Blobs,
): TranscriptEntry[] {
  const content = text(entry.content, blobs);
  if (content === undefined) return [];
  const kinds: Partial<Record<TranscriptChannel, TranscriptEntry['kind']>> = {
    [TranscriptChannel.ASSISTANT]: 'assistant',
    [TranscriptChannel.ANALYSIS]: 'analysis',
    [TranscriptChannel.DIAGNOSTIC]: 'diagnostic',
    [TranscriptChannel.PROMPT]: 'prompt',
  };
  const kind = kinds[entry.channel];
  if (!kind) throw new Error('Invalid transcript channel');
  return [{...base, kind, content}];
}
/** The typed payload a result carried apart from its display text; unknown shapes fail by name. */
function payloadOf(text: string): ToolResultPayload {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('Transcript tool payload is not JSON');
  }
  return validateToolResultPayload(value);
}
function callEntry(
  base: Base,
  entry: Extract<EntryBody, {case: 'toolCall'}>['value'],
  blobs: Blobs,
): TranscriptEntry[] {
  const args = text(entry.argumentsJson, blobs);
  if (args === undefined) return [];
  return [
    {
      ...base,
      kind: 'tool',
      content: '',
      startsTurn: true,
      toolName: entry.tool,
      ...(entry.callId ? {toolCallId: entry.callId} : {}),
      toolArguments: ownProjectionInput(argumentsOf(args)),
    },
  ];
}
function resultEntry(
  base: Base,
  entry: Extract<EntryBody, {case: 'toolResult'}>['value'],
  blobs: Blobs,
): TranscriptEntry[] {
  const content = text(entry.content, blobs);
  const payloadText = entry.payloadJson ? text(entry.payloadJson, blobs) : undefined;
  if (content === undefined || (entry.payloadJson && payloadText === undefined)) return [];
  const payload = payloadText === undefined ? undefined : payloadOf(payloadText);
  return [
    {
      ...base,
      kind: 'tool',
      content,
      toolName: entry.tool,
      ...(entry.callId ? {toolCallId: entry.callId} : {}),
      ...(entry.isError ? {tone: 'failure'} : {}),
      toolResult: ownProjectionInput({
        kind: 'tool_result',
        tool: entry.tool,
        content,
        is_error: entry.isError,
        call_id: entry.callId ?? null,
        ...(payload === undefined ? {} : {payload}),
      }),
    },
  ];
}

/** Stable byte-offset identities let the existing transcript fold join chunks and tool results. */
export function nativeTranscriptEntries(
  spanId: string,
  transcript: NativeTranscript,
  blobs: Blobs = {},
): readonly TranscriptEntry[] {
  return transcript.entries.flatMap(({offset, value}) => {
    const base = {
      id: `transcript:${spanId}:${offset}`,
      spanId,
      turnId: spanId,
      invocationId: spanId,
      ...(value.at === undefined ? {} : {at: timestampMs(value.at)}),
    };
    switch (value.entry.case) {
      case 'text':
        return textEntry(base, value.entry.value, blobs);
      case 'toolCall':
        return callEntry(base, value.entry.value, blobs);
      case 'toolResult':
        return resultEntry(base, value.entry.value, blobs);
      default:
        throw new Error('Transcript frame has no entry');
    }
  });
}
