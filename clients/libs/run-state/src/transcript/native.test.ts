import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {RUN_LOG_MAX_RECORD_BYTES, TranscriptChannel, TranscriptEntrySchema} from '@vibesys/proto';
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

const COMMAND_PAYLOAD = JSON.stringify({
  kind: 'command',
  stdout: 'out',
  stderr: 'err',
  exit_code: 2,
  duration: 1.5,
});

test('native calls and full results remain one tool with stable identities', () => {
  const encode = (entry: Parameters<typeof create<typeof TranscriptEntrySchema>>[1]) =>
    framed(toBinary(TranscriptEntrySchema, create(TranscriptEntrySchema, entry)));
  const call = encode({
    entry: {
      case: 'toolCall',
      value: {
        tool: 'Bash',
        callId: 'c1',
        argumentsJson: {value: {case: 'inline', value: '{"command":"cargo bench"}'}},
      },
    },
  });
  const result = encode({
    entry: {
      case: 'toolResult',
      value: {
        tool: 'Bash',
        callId: 'c1',
        isError: true,
        content: {value: {case: 'inline', value: 'display'}},
        payloadJson: {value: {case: 'inline', value: COMMAND_PAYLOAD}},
      },
    },
  });
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

function encodeEntry(
  entry: Parameters<typeof create<typeof TranscriptEntrySchema>>[1],
): Uint8Array {
  return framed(toBinary(TranscriptEntrySchema, create(TranscriptEntrySchema, entry)));
}

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
  for (const at of [0, 1, 999, 1_700_000_000_123, 4_102_444_800_000]) {
    const parsed = appendNativeTranscript(emptyNativeTranscript(), text(at));
    expect(nativeTranscriptEntries('turn-a', parsed).map(entry => entry.at)).toEqual([at]);
  }
  const unrecorded = appendNativeTranscript(emptyNativeTranscript(), text(undefined));
  const [entry] = nativeTranscriptEntries('turn-a', unrecorded);
  expect(entry).toBeDefined();
  expect('at' in (entry ?? {})).toBe(false);
});

test('a call keeps its own time when its result joins it', () => {
  const call = encodeEntry({
    entry: {
      case: 'toolCall',
      value: {
        tool: 'Bash',
        callId: 'c1',
        argumentsJson: {value: {case: 'inline', value: '{"command":"ls"}'}},
      },
    },
    at: timestampFromMs(1_000),
  });
  const result = encodeEntry({
    entry: {
      case: 'toolResult',
      value: {
        tool: 'Bash',
        callId: 'c1',
        content: {value: {case: 'inline', value: 'a'}},
        payloadJson: {value: {case: 'inline', value: COMMAND_PAYLOAD}},
      },
    },
    at: timestampFromMs(4_000),
  });
  const parsed = appendNativeTranscript(
    emptyNativeTranscript(),
    new Uint8Array([...call, ...result]),
  );
  const folded = transcriptBySpan(nativeTranscriptEntries('turn-a', parsed), new Set(['turn-a']));
  expect(folded.get('turn-a')?.map(entry => entry.at)).toEqual([1_000]);
});

test('a result payload that is not a known shape fails by name', () => {
  const withPayload = (payload: string) =>
    appendNativeTranscript(
      emptyNativeTranscript(),
      encodeEntry({
        entry: {
          case: 'toolResult',
          value: {
            tool: 'Bash',
            content: {value: {case: 'inline', value: 'a'}},
            payloadJson: {value: {case: 'inline', value: payload}},
          },
        },
      }),
    );
  expect(() => nativeTranscriptEntries('turn-a', withPayload('not json'))).toThrow('not JSON');
  expect(() => nativeTranscriptEntries('turn-a', withPayload('{"kind":"command"}'))).toThrow(
    'stdout',
  );
  expect(() => nativeTranscriptEntries('turn-a', withPayload('{"kind":"image"}'))).toThrow('kind');
  const json = nativeTranscriptEntries('turn-a', withPayload('{"kind":"json","value":[1]}'));
  expect(JSON.stringify(json.at(0)?.toolResult?.payload)).toBe('{"kind":"json","value":[1]}');
});
