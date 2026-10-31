import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  RUN_LOG_MAX_RECORD_BYTES,
  TranscriptChannel,
  TranscriptEntrySchema,
  TranscriptToolResultSchema,
} from '@vibesys/proto';
import * as fc from 'fast-check';
import {
  appendNativeTranscript,
  emptyNativeTranscript,
  nativeTranscriptEntries,
  nativeTranscriptHashes,
  transcriptBySpan,
} from './index.js';

function framed(payload: Uint8Array): Uint8Array {
  const prefix: number[] = [];
  let size = payload.length;
  while (size >= 128) {
    prefix.push((size & 127) | 128);
    size >>>= 7;
  }
  return new Uint8Array([...prefix, size, ...payload]);
}

test('native transcript framing preserves every entry and byte identity under every split', () => {
  for (const size of [0, 1, 126, 127, 128, 1024]) {
    const content = `unicode 🦊 ${'x'.repeat(size)}`;
    const wire = framed(
      toBinary(
        TranscriptEntrySchema,
        create(TranscriptEntrySchema, {
          entry: {
            case: 'text',
            value: {
              channel: TranscriptChannel.ASSISTANT,
              content: {value: {case: 'inline', value: content}},
            },
          },
        }),
      ),
    );
    const expected = appendNativeTranscript(emptyNativeTranscript(), wire);
    for (let split = 0; split <= wire.length; split += 1) {
      const first = appendNativeTranscript(emptyNativeTranscript(), wire.slice(0, split));
      const complete = appendNativeTranscript(first, wire.slice(split));
      expect(complete).toEqual(expected);
      expect(nativeTranscriptEntries('turn-a', complete)).toEqual([
        {
          id: 'transcript:turn-a:0',
          spanId: 'turn-a',
          turnId: 'turn-a',
          invocationId: 'turn-a',
          kind: 'assistant',
          content,
        },
      ]);
    }
  }
});

test('native blob references wait for their own content and malformed frames fail', () => {
  const hash = 'a'.repeat(64);
  const wire = framed(
    toBinary(
      TranscriptEntrySchema,
      create(TranscriptEntrySchema, {
        entry: {
          case: 'toolCall',
          value: {
            tool: 'Bash',
            callId: 'call-a',
            argumentsJson: {value: {case: 'blob', value: {sha256: hash}}},
          },
        },
      }),
    ),
  );
  const transcript = appendNativeTranscript(emptyNativeTranscript(), wire);
  expect(nativeTranscriptHashes(transcript)).toEqual([hash]);
  expect(nativeTranscriptEntries('turn-a', transcript)).toEqual([]);
  expect(
    nativeTranscriptEntries('turn-a', transcript, {[hash]: '{"command":"echo hello"}'}).at(0)
      ?.toolArguments,
  ).toEqual({command: 'echo hello'});
  expect(() => nativeTranscriptEntries('turn-a', transcript, {[hash]: '[]'})).toThrow('arguments');
  expect(() => appendNativeTranscript(emptyNativeTranscript(), new Uint8Array([0]))).toThrow(
    'Empty',
  );
  expect(() =>
    appendNativeTranscript(emptyNativeTranscript(), new Uint8Array([255, 255, 255, 255, 127])),
  ).toThrow('limit');
  expect(RUN_LOG_MAX_RECORD_BYTES).toBeGreaterThan(0);
});

const inline = (value: string) => ({value: {case: 'inline' as const, value}});
const blobOf = (sha256: string) => ({value: {case: 'blob' as const, value: {sha256}}});
const commandPayload = (
  stdout: string,
  stderr: string,
  exitCode?: number,
  durationSeconds?: number,
) => ({
  case: 'command' as const,
  value: {
    stdout: inline(stdout),
    stderr: inline(stderr),
    ...(exitCode === undefined ? {} : {exitCode}),
    ...(durationSeconds === undefined ? {} : {durationSeconds}),
  },
});

function encodeEntry(
  entry: Parameters<typeof create<typeof TranscriptEntrySchema>>[1],
): Uint8Array {
  return framed(toBinary(TranscriptEntrySchema, create(TranscriptEntrySchema, entry)));
}

const resultEntryOf = (payload: ReturnType<typeof commandPayload> | undefined) =>
  encodeEntry({
    entry: {
      case: 'toolResult',
      value: {
        tool: 'Bash',
        callId: 'c1',
        isError: true,
        content: inline('display'),
        ...(payload === undefined ? {} : {payload}),
      },
    },
  });

function entriesOf(wire: Uint8Array, blobs: Record<string, string> = {}) {
  return nativeTranscriptEntries(
    'turn-a',
    appendNativeTranscript(emptyNativeTranscript(), wire),
    blobs,
  );
}

test('native calls and full results remain one tool with stable identities', () => {
  const call = encodeEntry({
    entry: {
      case: 'toolCall',
      value: {
        tool: 'Bash',
        callId: 'c1',
        argumentsJson: {value: {case: 'inline', value: '{"command":"cargo bench"}'}},
      },
    },
  });
  const result = resultEntryOf(commandPayload('out', 'err', 2, 1.5));
  const wire = new Uint8Array([...call, ...result]);
  const whole = appendNativeTranscript(emptyNativeTranscript(), wire);
  for (let split = 0; split <= wire.length; split += 1) {
    const parsed = appendNativeTranscript(
      appendNativeTranscript(emptyNativeTranscript(), wire.slice(0, split)),
      wire.slice(split),
    );
    expect(parsed).toEqual(whole);
    const tools = transcriptBySpan(
      nativeTranscriptEntries('turn-a', parsed),
      new Set(['turn-a']),
    ).get('turn-a');
    expect(tools).toHaveLength(1);
    expect(tools?.[0]).toMatchObject({
      toolName: 'Bash',
      toolCallId: 'c1',
      tone: 'failure',
      toolResult: {
        content: 'display',
        payload: {kind: 'command', stdout: 'out', stderr: 'err', exit_code: 2, duration: 1.5},
      },
    });
  }
});

/** Output as a command prints it, and as a test must reach it (see the generator test below). */
const lines = fc.array(fc.string({unit: 'grapheme', maxLength: 12}), {maxLength: 6});
const outputText = fc.oneof(
  fc.string({unit: 'grapheme', maxLength: 40}),
  lines.map(each => each.join('\n')),
  lines.map(each => each.join('\r\n')),
  lines.map(each => `${each.join('\n')}\n`),
  fc.nat(5).map(count => '\n'.repeat(count)),
  fc.constant(''),
  fc.constantFrom('x'.repeat(65_536), '\u00e9'.repeat(40_000), 'line\n'.repeat(30_000)),
);

test('the output generator reaches newlines, CRLF, non-ASCII, the empty text and text over the inline cap', () => {
  const sample = fc.sample(outputText, {numRuns: 600, seed: 1});
  expect(sample.some(text => text.includes('\n'))).toBe(true);
  expect(sample.some(text => text.includes('\r\n'))).toBe(true);
  expect(sample.some(text => text.endsWith('\n'))).toBe(true);
  expect(sample.some(text => /\P{ASCII}/u.test(text))).toBe(true);
  expect(sample.some(text => text === '')).toBe(true);
  expect(sample.some(text => new TextEncoder().encode(text).length > 65_536)).toBe(true);
});

test('a command result reaches the entry as the parts it was written with, whatever they hold', () => {
  fc.assert(
    fc.property(
      outputText,
      outputText,
      fc.option(fc.integer({min: -(2 ** 31), max: 2 ** 31 - 1}), {nil: undefined}),
      fc.option(fc.double({min: 0, max: 1e9, noNaN: true}), {nil: undefined}),
      (stdout, stderr, exitCode, seconds) => {
        const [entry] = entriesOf(resultEntryOf(commandPayload(stdout, stderr, exitCode, seconds)));
        expect(entry?.content).toBe('display');
        expect(entry?.toolResult?.payload).toEqual({
          kind: 'command',
          stdout,
          stderr,
          exit_code: exitCode ?? null,
          duration: seconds ?? null,
        });
      },
    ),
  );
});

test('a command result whose output is a blob waits for it, and asks for each blob it needs', () => {
  const [out, err] = ['a'.repeat(64), 'b'.repeat(64)];
  const wire = resultEntryOf({
    case: 'command',
    value: {stdout: blobOf(out), stderr: blobOf(err), exitCode: 1},
  } as never);
  const parsed = appendNativeTranscript(emptyNativeTranscript(), wire);
  expect(nativeTranscriptHashes(parsed)).toEqual([out, err]);
  expect(nativeTranscriptEntries('turn-a', parsed, {})).toEqual([]);
  expect(nativeTranscriptEntries('turn-a', parsed, {[out]: 'x'})).toEqual([]);
  const [entry] = nativeTranscriptEntries('turn-a', parsed, {[out]: 'x\ny', [err]: 'boom'});
  expect(entry?.toolResult?.payload).toMatchObject({stdout: 'x\ny', stderr: 'boom', exit_code: 1});
});

function resultBytes(value: Parameters<typeof create<typeof TranscriptToolResultSchema>>[1]) {
  return toBinary(TranscriptToolResultSchema, create(TranscriptToolResultSchema, value));
}
function varint(value: number): number[] {
  const bytes: number[] = [];
  let rest = value;
  while (rest >= 128) {
    bytes.push((rest & 127) | 128);
    rest >>>= 7;
  }
  return [...bytes, rest];
}
const lengthDelimited = (field: number, body: Uint8Array): number[] => [
  (field << 3) | 2,
  ...varint(body.length),
  ...body,
];
/** A framed entry whose tool result is `result` followed by `extra` fields, as another version's writer sends it. */
function resultFrom(result: Uint8Array, extra: readonly number[]): Uint8Array {
  const body = new Uint8Array([...result, ...extra]);
  return framed(new Uint8Array([...lengthDelimited(3, body)]));
}
const TEXT_ONLY = {tool: 'Bash', callId: 'c1', content: inline('display')};
const utf8 = (text: string) => new TextEncoder().encode(text);

test('a result with a payload kind this reader does not know shows its text, and so does the rest of the turn', () => {
  // Field 8 is a payload case a newer writer may add.
  const unknown = resultFrom(resultBytes(TEXT_ONLY), lengthDelimited(8, utf8('future')));
  const after = encodeEntry({
    entry: {
      case: 'text',
      value: {channel: TranscriptChannel.ASSISTANT, content: inline('and then')},
    },
  });
  const entries = entriesOf(new Uint8Array([...unknown, ...after]));
  expect(entries.map(entry => entry.content)).toEqual(['display', 'and then']);
  expect(entries[0]?.toolResult?.payload).toBeUndefined();
});

test('a result written with the JSON string field of an earlier version shows its text, whatever that string holds', () => {
  // Field 5 was `payload_json`; records written with it stay readable.
  for (const legacy of ['{"kind":"image"}', 'not json', '{"kind":"command"}', '', '[]']) {
    // The old field held a TranscriptContent, whose `inline` is field 1.
    const content = new Uint8Array(lengthDelimited(1, utf8(legacy)));
    const wire = resultFrom(resultBytes(TEXT_ONLY), lengthDelimited(5, content));
    const [entry] = entriesOf(wire);
    expect(entry?.content).toBe('display');
    expect(entry?.toolResult?.payload).toBeUndefined();
  }
});

test('a command payload with a part missing, or JSON that is not an object or array, shows its text', () => {
  const noStdout = resultEntryOf({
    case: 'command',
    value: {stderr: inline('e')},
  } as never);
  expect(entriesOf(noStdout)[0]?.toolResult?.payload).toBeUndefined();
  for (const written of ['5', '"s"', 'null', '{broken', '']) {
    const wire = resultEntryOf({case: 'json', value: inline(written)} as never);
    const [entry] = entriesOf(wire);
    expect(entry?.content).toBe('display');
    expect(entry?.toolResult?.payload).toBeUndefined();
  }
  const json = entriesOf(resultEntryOf({case: 'json', value: inline('{"a":[1,2]}')} as never));
  expect(JSON.stringify(json[0]?.toolResult?.payload)).toBe('{"kind":"json","value":{"a":[1,2]}}');
});

test('native entries carry the time their event happened, and none when it was not recorded', () => {
  const text = (at: number | undefined) =>
    encodeEntry({
      entry: {
        case: 'text',
        value: {
          channel: TranscriptChannel.ANALYSIS,
          content: {value: {case: 'inline', value: 'thinking'}},
        },
      },
      ...(at === undefined ? {} : {at: timestampFromMs(at)}),
    });
  fc.assert(
    fc.property(fc.integer({min: 946_684_801_000, max: 4_102_444_800_000}), at => {
      expect(entriesOf(text(at)).map(entry => entry.at)).toEqual([at]);
    }),
  );
  const [entry] = entriesOf(text(undefined));
  expect(entry).toBeDefined();
  expect('at' in (entry ?? {})).toBe(false);
});

test('the epoch is how a time never set reads, so an entry holding only it has no time', () => {
  const text = (at: number) =>
    encodeEntry({
      entry: {
        case: 'text',
        value: {channel: TranscriptChannel.ANALYSIS, content: inline('thinking')},
      },
      at: timestampFromMs(at),
    });
  for (const at of [0, -1, -86_400_000]) {
    const [entry] = entriesOf(text(at));
    expect(entry?.content).toBe('thinking');
    expect('at' in (entry ?? {})).toBe(false);
  }
  expect(entriesOf(text(1))[0]?.at).toBe(1);
});

test('a call keeps its own time when its result joins it, whichever time is the later', () => {
  fc.assert(
    fc.property(
      fc.integer({min: 946_684_801_000, max: 4_102_444_800_000}),
      fc.integer({min: 946_684_801_000, max: 4_102_444_800_000}),
      (calledAt, answeredAt) => {
        const call = encodeEntry({
          entry: {
            case: 'toolCall',
            value: {tool: 'Bash', callId: 'c1', argumentsJson: inline('{"command":"ls"}')},
          },
          at: timestampFromMs(calledAt),
        });
        const result = encodeEntry({
          entry: {case: 'toolResult', value: {...TEXT_ONLY}},
          at: timestampFromMs(answeredAt),
        });
        const parsed = appendNativeTranscript(
          emptyNativeTranscript(),
          new Uint8Array([...call, ...result]),
        );
        const folded = transcriptBySpan(
          nativeTranscriptEntries('turn-a', parsed),
          new Set(['turn-a']),
        );
        expect(folded.get('turn-a')?.map(entry => entry.at)).toEqual([calledAt]);
      },
    ),
  );
});
