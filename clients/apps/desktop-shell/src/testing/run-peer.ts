import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {
  RecordSchema,
  type RunEntry,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
  TopicEntrySchema,
} from '@vibesys/proto';
import {type FakePortCall, FakeSubscriptionPort} from '@vibesys/subscription/testing';
import type {WireFixture} from './gateway-wire.js';

/** The run the window follows in these tests. */
export const RUN_ID = 'run-a';

/**
 * Main's side of the window's subscription to the host's runs: it says what the host says of the
 * run to each observation, as main relays a subscription that is delivering, and says it has
 * stopped delivering when asked. The window holds no lease and no timer of its own for this.
 */
export class LivenessHost {
  readonly port = new FakeSubscriptionPort();
  #delivering = true;
  #since = 0;

  constructor(public run: RunEntry) {
    const observe = this.port.observe;
    this.port.observe = (host, request, key, receive, ended) => {
      const handle = observe(host, request, key, receive, ended);
      // Main answers after the window has the handle, as it does over a port.
      queueMicrotask(() => {
        const call = this.port.calls.find(each => each.open && each.request === request);
        if (call !== undefined && this.#delivering) this.#say(call);
      });
      return handle;
    };
  }

  /** The observations of the run the window holds now. */
  get followers(): FakePortCall[] {
    return this.port.calls.filter(call => call.open);
  }

  #say(call: FakePortCall): void {
    this.#since += 1;
    call.send({kind: 'begin'});
    call.send({
      kind: 'entry',
      entry: create(TopicEntrySchema, {key: RUN_ID, value: {case: 'runs', value: this.run}}),
    });
    call.send({kind: 'synced'});
    call.send({kind: 'control', control: {kind: 'live', since: this.#since}});
  }

  /** The host says `run` of the run, on every observation: a stream that delivers. */
  publish(run: RunEntry = this.run): void {
    this.run = run;
    this.#delivering = true;
    for (const call of this.followers) this.#say(call);
  }

  /** The host's stream was lost or went silent: main's lease says so, and keeps what it had. */
  lose(): void {
    this.#delivering = false;
    for (const call of this.followers)
      call.send({
        kind: 'control',
        control: {kind: 'stale', reason: 'silent', attempt: 1, valueAt: this.#since},
      });
  }
}

/** The same protobuf peer as the app journeys, through the Electron byte bridge role. */
export class RunPeer extends FakeGatewayBridge {
  readonly host: LivenessHost;
  constructor(
    readonly input: WireFixture,
    readonly records: StoreRecord[],
    readonly loading: boolean,
  ) {
    super();
    this.host = new LivenessHost(input.run);
  }
  override unary(host: HostKey, method: string, request: Uint8Array) {
    const call = super.unary(host, method, request);
    const reply =
      method === this.input.methods.skeleton
        ? this.input.skeleton
        : method === this.input.methods.blobs
          ? this.input.blobs[[...request].join(',')]
          : undefined;
    if (method === this.input.methods.hostCall) return call;
    if (!reply) throw new Error(`Unexpected unary ${method}`);
    this.calls.at(-1)?.resolve(new Uint8Array(reply));
    return call;
  }
  override stream(
    host: HostKey,
    method: string,
    request: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string) => void,
  ) {
    const owned = super.stream(host, method, request, receive, ended);
    if (method === this.input.methods.tail) {
      const tail = fromBinary(TailRequestSchema, request);
      const records = this.records.filter(record => record.seq >= tail.fromSeq);
      if (!tail.sessionId && records.length > 0 && !(this.loading && tail.fromSeq > 0n))
        receive(
          toBinary(
            TailResponseSchema,
            create(TailResponseSchema, {
              records: records.map(record => toBinary(RecordSchema, record)),
              throughSeq: records.at(-1)?.seq ?? 0n,
            }),
          ),
        );
    } else if (method === this.input.methods.log) receive(new Uint8Array(this.input.log));
    else throw new Error(`Unexpected stream ${method}`);
    return owned;
  }
}
