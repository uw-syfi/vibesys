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

/** What a checkout that predates a command writes: argparse's usage dump. */
export const USAGE_DUMP =
  'usage: vibesys [-h] ...\nvibesys: error: unrecognized arguments: host list-examples';

/**
 * The commands whose absence the gateway reports as UNSUPPORTED ("needs an update"): the version
 * handshake itself. `proto/testdata/older_host/unsupported_commands.json` holds the same set, and
 * both the gateway's classifier and this Fake are asserted against it.
 */
export const COMMANDS_REPORTED_UNSUPPORTED: ReadonlySet<string> = new Set(['inspect-candidate']);

/** The host command a method runs: its oneof name, in kebab case. */
export function commandOf(methodName: string): string {
  return methodName.replaceAll('_', '-');
}

/** What the gateway answers when `command` exited with argparse's usage status on an older host. */
function failure(command: string): {category: FailureCategory; message: string} {
  if (COMMANDS_REPORTED_UNSUPPORTED.has(command)) {
    return {
      category: FailureCategory.UNSUPPORTED,
      message: `this host's VibeSys does not provide "${command}"; update it`,
    };
  }
  return {
    category: FailureCategory.INTERNAL,
    message: `vibesys host ${command} exited 2\n${USAGE_DUMP}`,
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
 * answered as the gateway answers a checkout that lacks its command. Only the version handshake is
 * UNSUPPORTED; any other command fails as an ordinary failure with the checkout's usage dump in its
 * diagnostic.
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
