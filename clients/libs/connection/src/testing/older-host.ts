import {create, type DescMessage, fromBinary, isFieldSet, toBinary} from '@bufbuild/protobuf';
import {
  FailureCategory,
  MutateRequestSchema,
  MutateResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
  WatchRequestSchema,
  WatchResponseSchema,
} from '@vibesys/proto';

/** The host command a method runs: its oneof name, in kebab case. */
export function commandOf(methodName: string): string {
  return methodName.replaceAll('_', '-');
}

/** What the gateway answers when a checkout rejects `command` as unknown. */
function failure(command: string): {category: FailureCategory; message: string} {
  return {
    category: FailureCategory.UNSUPPORTED,
    message: `this host's VibeSys does not provide "${command}"; update it`,
  };
}

function commandInRequest(schema: DescMessage, bytes: Uint8Array): string {
  const request = fromBinary(schema, bytes);
  const method = schema.oneofs.find(oneof => oneof.name === 'method');
  const field = method?.fields.find(candidate => isFieldSet(request, candidate));
  if (field === undefined) throw new Error('the request names no method');
  return commandOf(field.name);
}

/**
 * A host whose VibeSys predates every registry command, at the gateway's ports: each method is
 * answered as the gateway answers a checkout that lacks its command, UNSUPPORTED. The recorded
 * argparse output of a real older checkout (`proto/testdata/older_host/older_host.json`) is what
 * the gateway's classifier is asserted against; `older-host.test.ts` asserts that this Fake
 * answers exactly the commands that corpus rejects.
 */
export const olderHost = {
  read: async (_host: unknown, bytes: Uint8Array): Promise<Uint8Array> =>
    toBinary(
      ReadResponseSchema,
      create(ReadResponseSchema, {
        result: {case: 'failure', value: failure(commandInRequest(ReadRequestSchema, bytes))},
      }),
    ),
  mutate: async (_host: unknown, bytes: Uint8Array): Promise<Uint8Array> =>
    toBinary(
      MutateResponseSchema,
      create(MutateResponseSchema, {
        result: {case: 'failure', value: failure(commandInRequest(MutateRequestSchema, bytes))},
      }),
    ),
  watch: async (
    _host: unknown,
    bytes: Uint8Array,
    _signal: AbortSignal,
    receive: (payload: Uint8Array) => void,
  ): Promise<void> => {
    receive(
      toBinary(
        WatchResponseSchema,
        create(WatchResponseSchema, {
          result: {case: 'failure', value: failure(commandInRequest(WatchRequestSchema, bytes))},
        }),
      ),
    );
  },
};
