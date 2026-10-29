import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
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
        payloadJson: {value: {case: 'inline', value: 'complete result'}},
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
      toolResult: {content: 'complete result'},
    });
  }
});
