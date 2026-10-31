/** Pure decoding and projection of the run log's native transcript stream. */
import {fromBinary, type MessageShape} from '@bufbuild/protobuf';
import {timestampMs} from '@bufbuild/protobuf/wkt';
import {
  RUN_LOG_MAX_RECORD_BYTES,
  TranscriptChannel,
  type TranscriptContent,
  TranscriptEntrySchema,
} from '@vibesys/proto';
import {ownProjectionInput} from '../publication.js';
import type {ToolResultPayload, TranscriptEntry} from '../transcript-fold.js';

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

type ToolResultBody = Extract<NativeEntry['entry'], {case: 'toolResult'}>['value'];

/** The contents a result's typed payload holds; none when it has no payload or one this reader does not know. */
function payloadContents(result: ToolResultBody): readonly (TranscriptContent | undefined)[] {
  const payload = result.payload;
  switch (payload.case) {
    case 'command':
      return [payload.value.stdout, payload.value.stderr];
    case 'json':
      return [payload.value];
    default:
      return [];
  }
}

function contents(entry: NativeEntry): readonly (TranscriptContent | undefined)[] {
  switch (entry.entry.case) {
    case 'text':
      return [entry.entry.value.content];
    case 'toolCall':
      return [entry.entry.value.argumentsJson];
    case 'toolResult':
      return [entry.entry.value.content, ...payloadContents(entry.entry.value)];
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

/** The entry's time, none when the record has none or holds the epoch itself (the unset time). */
function eventTime(at: NativeEntry['at']): {at?: number} {
  if (at === undefined) return {};
  const ms = timestampMs(at);
  return ms > 0 ? {at: ms} : {};
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
/** A result's payload once its contents are known; `waiting` while one is still a blob nobody has sent. */
type PayloadReading =
  | {readonly state: 'waiting'}
  | {readonly state: 'ready'; readonly payload?: ToolResultPayload};

const NO_PAYLOAD: PayloadReading = {state: 'ready'};

type JsonResultValue = Extract<ToolResultPayload, {kind: 'json'}>['value'];

/** A JSON object or array in `value`, or undefined for any other text. */
function jsonValueOf(value: string): JsonResultValue | undefined {
  try {
    const parsed: unknown = JSON.parse(value);
    // Parsed JSON is a JSON value by construction.
    return typeof parsed === 'object' && parsed !== null ? (parsed as JsonResultValue) : undefined;
  } catch {
    return undefined;
  }
}

/**
 * What a result carried beside its text. The deprecated `payloadJson` field (the JSON string an
 * earlier writer stored) is never read: such a record shows its `content`.
 * A payload this reader cannot use (a case a newer writer
 * added, a part the writer left out, JSON text that is not an object or array) is none: the result
 * shows its text alone, and the rest of the turn is read as before.
 */
function payloadOf(result: ToolResultBody, blobs: Blobs): PayloadReading {
  const payload = result.payload;
  if (payload.case === 'command') {
    const {stdout, stderr} = payload.value;
    if (stdout === undefined || stderr === undefined) return NO_PAYLOAD;
    const out = text(stdout, blobs);
    const err = text(stderr, blobs);
    if (out === undefined || err === undefined) return {state: 'waiting'};
    return {
      state: 'ready',
      payload: {
        kind: 'command',
        stdout: out,
        stderr: err,
        exit_code: payload.value.exitCode ?? null,
        duration: payload.value.durationSeconds ?? null,
      },
    };
  }
  if (payload.case === 'json') {
    const written = text(payload.value, blobs);
    if (written === undefined) return {state: 'waiting'};
    const value = jsonValueOf(written);
    return value === undefined ? NO_PAYLOAD : {state: 'ready', payload: {kind: 'json', value}};
  }
  return NO_PAYLOAD;
}

function resultEntry(base: Base, entry: ToolResultBody, blobs: Blobs): TranscriptEntry[] {
  const content = text(entry.content, blobs);
  const reading = payloadOf(entry, blobs);
  if (content === undefined || reading.state === 'waiting') return [];
  const {payload} = reading;
  return [
    {
      ...base,
      kind: 'tool',
      content,
      toolName: entry.tool,
      ...(entry.callId ? {toolCallId: entry.callId} : {}),
      ...(entry.isError ? {tone: 'failure'} : {}),
      toolResult: {
        ...ownProjectionInput({
          kind: 'tool_result',
          tool: entry.tool,
          content,
          is_error: entry.isError,
          call_id: entry.callId ?? null,
        }),
        ...(payload === undefined ? {} : {payload}),
      },
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
      ...eventTime(value.at),
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
