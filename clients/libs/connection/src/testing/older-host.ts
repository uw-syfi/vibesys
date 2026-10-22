import {create, toBinary} from '@bufbuild/protobuf';
import {
  FailureCategory,
  MutateResponseSchema,
  ReadResponseSchema,
  WatchResponseSchema,
} from '@vibesys/proto';

/** What a checkout that predates a command writes: argparse's usage dump. */
export const USAGE_DUMP =
  'usage: vibesys [-h] ...\nvibesys: error: unrecognized arguments: host list-examples';

const failure = {category: FailureCategory.UNSUPPORTED, message: USAGE_DUMP};

/**
 * A host whose VibeSys answers UNSUPPORTED to every registry operation, at the gateway's ports, with
 * the usage dump in its diagnostic. The real gateway sends this only for the version handshake; the
 * fake is broader so each client's handling of the category is exercised. A client must not show the dump.
 */
export const olderHost = {
  read: async (): Promise<Uint8Array> =>
    toBinary(
      ReadResponseSchema,
      create(ReadResponseSchema, {result: {case: 'failure', value: failure}}),
    ),
  mutate: async (): Promise<Uint8Array> =>
    toBinary(
      MutateResponseSchema,
      create(MutateResponseSchema, {result: {case: 'failure', value: failure}}),
    ),
  watch: async (
    _host: unknown,
    _bytes: Uint8Array,
    _signal: AbortSignal,
    receive: (payload: Uint8Array) => void,
  ): Promise<void> => {
    receive(
      toBinary(
        WatchResponseSchema,
        create(WatchResponseSchema, {result: {case: 'failure', value: failure}}),
      ),
    );
  },
};
