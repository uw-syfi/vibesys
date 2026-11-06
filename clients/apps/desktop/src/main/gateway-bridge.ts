/**
 * The host executable's stdio bridge as one duplex. `vibesys-host connect` finds the host (and which
 * socket it is on), starts or replaces it when it must, and pipes its own stdin and stdout to it, so
 * a client of any machine needs no knowledge of where the host listens.
 */
import {Duplex} from 'node:stream';
import * as Effect from 'effect/Effect';
import type * as Scope from 'effect/Scope';
import type {SpawnedProcess} from './process.js';

/** The newest bytes of a bridge process's stderr kept for the log. */
const STDERR_TAIL = 4096;

/** How the way the bridge was run puts into words that it ended. */
export interface BridgeExplanation {
  readonly explain: (
    code: number | null,
    stderr: string,
    where: string,
  ) => {readonly message: string};
}

/**
 * The gateway process's pipes as one duplex. When the process ends, the duplex is destroyed and
 * the explanation for how it ended, with the tail of what the process said, goes to `log`.
 */
export const bridge = (
  process: SpawnedProcess,
  link: BridgeExplanation,
  log: (line: string) => void,
): Effect.Effect<Duplex, never, Scope.Scope> =>
  Effect.gen(function* () {
    const duplex = Duplex.from({readable: process.stdout, writable: process.stdin});
    // Destroying the pipes ends the duplex with an abort error that no one is left to read; the
    // session on top owns real failures.
    duplex.on('error', () => {});
    yield* Effect.addFinalizer(() => Effect.sync(() => duplex.destroy()));
    let stderr = '';
    process.stderr.setEncoding('utf8');
    process.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-STDERR_TAIL);
    });
    yield* Effect.forkScoped(
      Effect.flatMap(process.exit, code =>
        Effect.sync(() => {
          const tail = stderr.trim();
          const cause = link.explain(code, tail, 'the gateway').message;
          const said = tail === '' ? 'no output' : tail;
          log(`gateway connect exited: ${code} (${cause})\n${said}`);
          duplex.destroy();
        }),
      ),
    );
    return duplex;
  });
