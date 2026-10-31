import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  DesktopConnectStep,
  DesktopRetryRequestSchema,
  DesktopSubscribeRequestSchema,
  DesktopSubscriptionReason,
  DesktopSubscriptionRequestSchema,
  MachineStatsSchema,
  TopicEntrySchema,
} from '@vibesys/proto';
import {
  CONNECT_STEPS,
  type Control,
  machineStats,
  RETRYABLE_REASONS,
  type SubscriptionUpdate,
  TERMINAL_REASONS,
} from '@vibesys/subscription';
import fc from 'fast-check';
import {gatewayEnd, gatewayReply} from './gateway.js';
import {
  decodeUpdate,
  encodeSubscriptionCall,
  encodeUpdate,
  parseSubscriptionCall,
  subscriptionBridge,
} from './subscription.js';

const STEP = fc.constantFrom(...CONNECT_STEPS);
const ATTEMPT = fc.nat({max: 1000});
const CONTROL: fc.Arbitrary<Control> = fc.oneof(
  fc.record({kind: fc.constant('connecting' as const), step: STEP, attempt: ATTEMPT}),
  fc.record({kind: fc.constant('live' as const), since: fc.integer({min: 0, max: 4e12})}),
  fc.record({
    kind: fc.constant('stale' as const),
    reason: fc.constantFrom(...RETRYABLE_REASONS),
    attempt: ATTEMPT,
    valueAt: fc.integer({min: 0, max: 4e12}),
  }),
  fc.record({
    kind: fc.constant('failed' as const),
    reason: fc.constantFrom(...RETRYABLE_REASONS, ...TERMINAL_REASONS),
    retrying: fc.boolean(),
  }),
);

function entry(key: string, sampledMs: number) {
  return create(TopicEntrySchema, {
    key,
    value: {
      case: 'machineStats',
      value: create(MachineStatsSchema, {
        sampledAt: timestampFromMs(sampledMs),
        cpu: {case: 'cpuUnavailable', value: {reason: 1}},
        memory: {case: 'memoryUnavailable', value: {reason: 1}},
        gpu: {case: 'gpuUnavailable', value: {reason: 1}},
      }),
    },
  });
}

const UPDATE: fc.Arbitrary<SubscriptionUpdate> = fc.oneof(
  CONTROL.map((control): SubscriptionUpdate => ({kind: 'control', control})),
  fc.constant<SubscriptionUpdate>({kind: 'begin'}),
  fc.constant<SubscriptionUpdate>({kind: 'synced'}),
  fc
    .string({minLength: 1, maxLength: 128})
    .map((key): SubscriptionUpdate => ({kind: 'removed', key})),
  fc
    .tuple(fc.string({minLength: 1, maxLength: 128}), fc.integer({min: 1, max: 4e12}))
    .map(([key, at]): SubscriptionUpdate => ({kind: 'entry', entry: entry(key, at)})),
);

test('property: every update crosses the port as itself, and its bytes are canonical', () => {
  fc.assert(
    fc.property(UPDATE, update => {
      const bytes = encodeUpdate(update);
      const decoded = decodeUpdate(bytes);
      expect(encodeUpdate(decoded)).toEqual(bytes);
      expect(decoded.kind).toBe(update.kind);
      if (update.kind === 'control') expect(decoded).toEqual(update);
      if (update.kind === 'removed') expect(decoded).toEqual(update);
    }),
  );
});

test('the closed sets are the generated enums, one for one', () => {
  const numbered = (enumeration: object): number[] =>
    Object.values(enumeration).filter(
      (each): each is number => typeof each === 'number' && each !== 0,
    );
  const control = (reason: (typeof RETRYABLE_REASONS)[number]): Control => ({
    kind: 'stale',
    reason,
    attempt: 1,
    valueAt: 1,
  });
  // Every reason survives the wire, and every enum value stands for exactly one reason.
  const seen = new Set<string>();
  for (const reason of [...RETRYABLE_REASONS, ...TERMINAL_REASONS]) {
    const update = decodeUpdate(
      encodeUpdate({
        kind: 'control',
        control:
          reason in {unsupported: 1, invalid_frame: 1, closed: 1}
            ? {kind: 'failed', reason, retrying: false}
            : control(reason as (typeof RETRYABLE_REASONS)[number]),
      }),
    );
    seen.add(update.kind === 'control' && 'reason' in update.control ? update.control.reason : '');
  }
  expect(seen.size).toBe(numbered(DesktopSubscriptionReason).length);
  const steps = new Set(
    CONNECT_STEPS.map(step => {
      const update = decodeUpdate(
        encodeUpdate({kind: 'control', control: {kind: 'connecting', step, attempt: 0}}),
      );
      return update.kind === 'control' && update.control.kind === 'connecting'
        ? update.control.step
        : '';
    }),
  );
  expect(steps.size).toBe(numbered(DesktopConnectStep).length);
});

test('an update with no case, a missing state or a bad value is refused by name', () => {
  expect(() => decodeUpdate(new Uint8Array())).toThrow();
  expect(() => decodeUpdate(new Uint8Array([0x0a, 0x00]))).toThrow();
  const stale = encodeUpdate({
    kind: 'control',
    control: {kind: 'stale', reason: 'silent', attempt: 1, valueAt: 1},
  });
  expect(() => decodeUpdate(new Uint8Array([...stale, 0xf8, 0x01, 1]))).toThrow();
});

test('a request names a host, a known topic and optionally a key, and nothing more', () => {
  const call = {
    kind: 'observe',
    host: 'ssh:host-a',
    request: machineStats.request,
    key: 'machine',
  } as const;
  const parsed = parseSubscriptionCall(encodeSubscriptionCall(call));
  if (parsed.kind !== 'observe') throw new Error('not an observation');
  expect(parsed.host).toBe('ssh:host-a');
  expect(parsed.key).toBe('machine');
  expect(parsed.request.topic.case).toBe('machineStats');
  const keyless = parseSubscriptionCall(encodeSubscriptionCall({...call, key: undefined}));
  expect(keyless.kind === 'observe' && keyless.key).toBeUndefined();
  expect(() => parseSubscriptionCall({})).toThrow('protobuf bytes');
  const without = toBinary(
    DesktopSubscriptionRequestSchema,
    create(DesktopSubscriptionRequestSchema, {
      request: {
        case: 'observe',
        value: create(DesktopSubscribeRequestSchema, {host: 'local'}),
      },
    }),
  );
  expect(() => parseSubscriptionCall(without)).toThrow();
  for (const host of ['', '-oProxyCommand=x', 'ssh:']) {
    expect(() => encodeSubscriptionCall({...call, host: host as never})).toThrow();
  }
  expect(() =>
    parseSubscriptionCall(new Uint8Array([...encodeSubscriptionCall(call), 0xf8, 0x01, 1])),
  ).toThrow();
  expect(() => encodeSubscriptionCall({...call, key: ''})).toThrow();
});

test('a retry names one host and nothing else; the port refuses every other request', () => {
  const retry = encodeSubscriptionCall({kind: 'retry', host: 'ssh:host-a'});
  expect(parseSubscriptionCall(retry)).toEqual({kind: 'retry', host: 'ssh:host-a'});
  for (const host of ['', '-oProxyCommand=x', 'ssh:', 'ssh:a b']) {
    expect(() => encodeSubscriptionCall({kind: 'retry', host: host as never})).toThrow();
  }
  expect(() => parseSubscriptionCall(new Uint8Array([...retry, 0xf8, 0x01, 1]))).toThrow();
  // A request that holds no case, and a retry whose host is not a host, never reach the hub.
  expect(() => parseSubscriptionCall(new Uint8Array())).toThrow();
  const hostless = toBinary(
    DesktopSubscriptionRequestSchema,
    create(DesktopSubscriptionRequestSchema, {
      request: {case: 'retry', value: create(DesktopRetryRequestSchema, {host: 'x y'})},
    }),
  );
  expect(() => parseSubscriptionCall(hostless)).toThrow();
});

class Ports {
  readonly calls: Array<{
    request: Uint8Array;
    reply(bytes: Uint8Array): void;
    fail(error: string): void;
    cancelled: number;
  }> = [];
  open(request: Uint8Array, reply: (bytes: Uint8Array) => void, fail: (error: string) => void) {
    const call = {request, reply, fail, cancelled: 0};
    this.calls.push(call);
    return {
      cancel: () => {
        call.cancelled += 1;
      },
    };
  }
}

test('the bridge opens one port per observation, decodes what arrives and retires the port once', () => {
  const ports = new Ports();
  const heard: SubscriptionUpdate[] = [];
  const ends: Array<string | undefined> = [];
  const call = subscriptionBridge(ports).observe(
    'ssh:host-a',
    machineStats.request,
    undefined,
    update => heard.push(update),
    error => ends.push(error),
  );
  const [open] = ports.calls;
  if (open === undefined) throw new Error('no port');
  expect(parseSubscriptionCall(open.request)).toMatchObject({
    kind: 'observe',
    host: 'ssh:host-a',
  });
  open.reply(gatewayReply(encodeUpdate({kind: 'begin'})));
  open.reply(gatewayReply(encodeUpdate({kind: 'control', control: {kind: 'live', since: 5}})));
  expect(heard.map(update => update.kind)).toEqual(['begin', 'control']);
  open.reply(gatewayEnd('closed by main'));
  open.reply(gatewayReply(encodeUpdate({kind: 'synced'})));
  call.cancel();
  expect(heard).toHaveLength(2);
  expect(ends).toEqual(['closed by main']);
  expect(open.cancelled).toBe(1);
});

test('a cancelled, malformed or undeliverable port ends the observation once and retires the port', () => {
  for (const outcome of ['cancel', 'malformed', 'delivery', 'open'] as const) {
    const ports = new Ports();
    const ends: Array<string | undefined> = [];
    const bridge = subscriptionBridge(
      outcome === 'open'
        ? {
            open: () => {
              throw new Error('cannot open');
            },
          }
        : ports,
    );
    const call = bridge.observe(
      'local',
      machineStats.request,
      undefined,
      () => {},
      error => ends.push(error),
    );
    const remote = ports.calls[0];
    if (outcome === 'cancel') call.cancel();
    if (outcome === 'malformed') remote?.reply(new Uint8Array([255]));
    if (outcome === 'delivery') remote?.fail('delivery failed');
    remote?.reply(gatewayReply(encodeUpdate({kind: 'synced'})));
    call.cancel();
    expect(ends).toHaveLength(outcome === 'cancel' ? 0 : 1);
    if (remote !== undefined) expect(remote.cancelled).toBe(1);
  }
});

test('retry opens a port that carries the retry and nothing else, and retires it when main ends it', () => {
  const ports = new Ports();
  subscriptionBridge(ports).retry('ssh:host-a');
  const [open] = ports.calls;
  if (open === undefined) throw new Error('no port');
  expect(parseSubscriptionCall(open.request)).toEqual({kind: 'retry', host: 'ssh:host-a'});
  expect(open.cancelled).toBe(0);
  open.reply(gatewayEnd());
  expect(open.cancelled).toBe(1);
});

test('a retry the window cannot make is dropped, and a port that fails retires itself', () => {
  expect(() =>
    subscriptionBridge({
      open: () => {
        throw new Error('cannot open');
      },
    }).retry('local'),
  ).not.toThrow();
  const ports = new Ports();
  subscriptionBridge(ports).retry('local');
  ports.calls[0]?.fail('delivery failed');
  expect(ports.calls[0]?.cancelled).toBe(1);
});
