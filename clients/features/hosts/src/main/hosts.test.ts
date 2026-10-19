import {describe, expect, test} from 'bun:test';
import {join} from 'node:path';
import {type ConnectionLease, ConnectionManager} from '@vibesys/connection';
import {FAKE_MODEL_CATALOGUE, FakeLink, record} from '@vibesys/connection/testing';
import type {RunSummary} from '@vibesys/contracts/host-helper';
import type {HostId, HostListing} from '@vibesys/hosts';
import {remember, SettingsStore, settingsFor, withHostSettings} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {Hosts, type SshConfigFilesystem} from './index.js';

const SSH_CONFIG = '/home/test/.ssh/config';
const LOCAL = {kind: 'local'} as const;
const ALPHA = {kind: 'ssh', alias: 'alpha'} as const;
const BETA = {kind: 'ssh', alias: 'beta'} as const;
const CONFIG = {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000};

type LeaseFailureMode = 'reject' | 'throw';

/** A real manager whose first public lease reports a failed retirement after releasing its hold. */
class FirstReleaseFailureManager extends ConnectionManager {
  readonly failure = new Error('retirement remained unconfirmed');
  readonly #mode: LeaseFailureMode;
  releases = 0;

  constructor(mode: LeaseFailureMode, options: ConstructorParameters<typeof ConnectionManager>[0]) {
    super(options);
    this.#mode = mode;
  }

  override async acquire(host: HostId, checkout: string): Promise<ConnectionLease> {
    const lease = await super.acquire(host, checkout);
    let released = false;
    return {
      connection: lease.connection,
      [Symbol.asyncDispose]: () => {
        if (released) return Promise.resolve();
        released = true;
        const retirement = lease[Symbol.asyncDispose]();
        this.releases += 1;
        if (this.releases === 1) {
          if (this.#mode === 'throw') {
            void Promise.resolve(retirement).catch(() => {});
            throw this.failure;
          }
          return retirement.then(() => {
            throw this.failure;
          });
        }
        return retirement;
      },
    };
  }
}

/** A real manager that withholds one acquired lease after its shown connection already changed. */
class DeferredAcquireManager extends ConnectionManager {
  readonly releasedCheckouts: string[] = [];
  #gate: AcquireGate | null = null;

  override async acquire(host: HostId, checkout: string): Promise<ConnectionLease> {
    await this.#wait(checkout, 'before');
    const lease = await super.acquire(host, checkout);
    await this.#wait(checkout, 'after');
    let released = false;
    return {
      connection: lease.connection,
      [Symbol.asyncDispose]: async () => {
        if (released) return;
        released = true;
        this.releasedCheckouts.push(checkout);
        await lease[Symbol.asyncDispose]();
      },
    };
  }

  deferNext(
    checkout: string,
    position: AcquireGate['position'] = 'after',
  ): {readonly received: Promise<void>; readonly release: () => void} {
    if (this.#gate !== null) throw new Error('an acquisition is already deferred');
    let arrived = (): void => {};
    let release = (): void => {};
    const received = new Promise<void>(resolve => {
      arrived = resolve;
    });
    const released = new Promise<void>(resolve => {
      release = resolve;
    });
    this.#gate = {checkout, position, arrived, released};
    return {received, release};
  }

  async #wait(checkout: string, position: AcquireGate['position']): Promise<void> {
    const gate = this.#gate;
    if (gate?.checkout !== checkout || gate.position !== position) return;
    this.#gate = null;
    gate.arrived();
    await gate.released;
  }
}

interface AcquireGate {
  readonly checkout: string;
  readonly position: 'before' | 'after';
  readonly arrived: () => void;
  readonly released: Promise<void>;
}

/** An explicit SSH-config read result, with every requested path recorded. */
class FakeSshConfigFilesystem implements SshConfigFilesystem {
  readonly reads: string[] = [];

  constructor(readonly result: {readonly text: string} | {readonly error: unknown}) {}

  async readText(path: string, _maximumBytes: number): Promise<string> {
    this.reads.push(path);
    if ('error' in this.result) throw this.result.error;
    return this.result.text;
  }

  async realPath(path: string): Promise<string> {
    return path;
  }

  async *directoryEntries(_path: string): AsyncIterable<string> {
    // This focused Fake has no included files.
  }
}

function fileError(code: string): Error {
  return Object.assign(new Error(`${code}: ${SSH_CONFIG}`), {code});
}

/** Hold exactly one numbered settings read so another intent can deterministically pass it. */
class DeferredReadFilesystem extends FakeSettingsFilesystem {
  readonly received: Promise<void>;
  readonly #received: () => void;
  readonly #released: Promise<void>;
  readonly #release: () => void;
  readonly #heldRead: number;
  #reads = 0;

  constructor(files: Readonly<Record<string, string>>, heldRead = 1) {
    super(files);
    this.#heldRead = heldRead;
    let received = (): void => {};
    let release = (): void => {};
    this.received = new Promise(resolve => {
      received = resolve;
    });
    this.#released = new Promise(resolve => {
      release = resolve;
    });
    this.#received = received;
    this.#release = release;
  }

  override async readText(path: string): Promise<string> {
    this.#reads += 1;
    if (this.#reads === this.#heldRead) {
      this.#received();
      await this.#released;
    }
    return super.readText(path);
  }

  release(): void {
    this.#release();
  }
}

/** Hold one read after its bytes were captured, while later reads and writes see newer bytes. */
class CapturedReadFilesystem extends FakeSettingsFilesystem {
  #gate: CapturedReadGate | null = null;

  override async readText(path: string): Promise<string> {
    const text = await super.readText(path);
    const gate = this.#gate;
    if (gate !== null) {
      this.#gate = null;
      gate.captured();
      await gate.released;
    }
    return text;
  }

  deferNextRead(): {readonly captured: Promise<void>; readonly release: () => void} {
    if (this.#gate !== null) throw new Error('a settings read is already deferred');
    let captured = (): void => {};
    let release = (): void => {};
    const reached = new Promise<void>(resolve => {
      captured = resolve;
    });
    const released = new Promise<void>(resolve => {
      release = resolve;
    });
    this.#gate = {captured, released};
    return {captured: reached, release};
  }
}

interface CapturedReadGate {
  readonly captured: () => void;
  readonly released: Promise<void>;
}

async function configuredSettings(
  settings: SettingsStore,
  hosts: readonly HostId[],
): Promise<void> {
  for (const host of hosts) {
    await settings.updateHosts(current =>
      withHostSettings(current, host, {
        checkout: `/checkouts/${host.kind === 'local' ? 'local' : host.alias}`,
      }),
    );
  }
}

/** The public Hosts feature over a Fake SSH-config filesystem; no connection is opened. */
function subject(filesystem: SshConfigFilesystem): Hosts {
  const manager = new ConnectionManager({
    linkFor: () => {
      throw new Error('aliases must not open a host connection');
    },
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
  const settings = new SettingsStore({
    settingsPath: '/unused/hosts.json',
    recentPath: '/unused/recent.json',
    preferencesPath: '/unused/preferences.json',
    log: () => {},
  });
  return new Hosts({
    manager,
    settings,
    localCheckout: '/vibesys',
    sshConfigPath: SSH_CONFIG,
    sshConfigFilesystem: filesystem,
    log: () => {},
    push: () => {},
    retire: () => {},
  });
}

/** The public completion controller over one in-memory helper and settings store. */
function completionSubject(link: FakeLink): {
  readonly hosts: Hosts;
  readonly manager: ConnectionManager;
} {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  const manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    config: CONFIG,
    log: () => {},
  });
  return {
    manager,
    hosts: new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    }),
  };
}

describe('Hosts.aliases', () => {
  test('reads and parses the configured SSH file', async () => {
    const filesystem = new FakeSshConfigFilesystem({text: 'Host gpu-box gpu-* jump\n'});

    expect(await subject(filesystem).aliases()).toEqual(['gpu-box', 'jump']);
    expect(filesystem.reads).toEqual([SSH_CONFIG]);
  });

  test('treats only a missing SSH config as an empty alias list', async () => {
    const filesystem = new FakeSshConfigFilesystem({error: fileError('ENOENT')});

    expect(await subject(filesystem).aliases()).toEqual([]);
    expect(filesystem.reads).toEqual([SSH_CONFIG]);
  });

  test('surfaces every other SSH config read failure unchanged', async () => {
    for (const code of ['EACCES', 'EISDIR', 'EIO']) {
      const error = fileError(code);
      const filesystem = new FakeSshConfigFilesystem({error});

      try {
        await subject(filesystem).aliases();
        throw new Error(`${code} unexpectedly resolved`);
      } catch (caught) {
        expect(caught).toBe(error);
      }
      expect(filesystem.reads).toEqual([SSH_CONFIG]);
    }
  });
});

describe('Hosts.complete', () => {
  test('returns a typed unsupported outcome without sending paths.complete', async () => {
    const link = new FakeLink();
    link.helper.features.delete('paths_complete');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.complete(LOCAL, '/manual/')).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'paths.complete')).toEqual([]);
  });

  test('returns a typed listing when paths_complete was negotiated', async () => {
    const link = new FakeLink();
    link.helper.host.directories.set('/manual', ['project']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.complete(LOCAL, '/manual/')).resolves.toEqual({
      kind: 'listed',
      directory: '/manual',
      entries: [
        {
          name: 'project',
          path: '/manual/project',
          checkout: false,
          project: false,
        },
      ],
      truncated: false,
    });
    expect(link.helper.requests.filter(request => request.type === 'paths.complete')).toHaveLength(
      1,
    );
  });
});

describe('Hosts.harnesses', () => {
  test('returns a typed unsupported outcome without sending harnesses.list', async () => {
    const link = new FakeLink();
    link.helper.features.delete('harnesses');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.harnesses(LOCAL)).resolves.toEqual({kind: 'unsupported'});
    expect(link.helper.requests.filter(request => request.type === 'harnesses.list')).toEqual([]);
  });

  test("returns the host's vetted pairs, each provider's default marked", async () => {
    const link = new FakeLink();
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.harnesses(LOCAL)).resolves.toEqual({
      kind: 'listed',
      harnesses: link.helper.host.harnesses.map(({provider, model, default: isDefault}) => ({
        provider,
        model,
        default: isDefault,
      })),
    });
  });
});

describe('Hosts.harnesses display', () => {
  test('carries how each pair reads, a pair the model is unnamed for with only the provider name, and none for a pair without it', async () => {
    const link = new FakeLink();
    link.helper.host.harnesses.splice(
      0,
      link.helper.host.harnesses.length,
      {
        provider: 'claude',
        model: 'claude-opus-5-5',
        default: true,
        display: {
          provider_name: 'Claude Code',
          model_name: 'Opus 5.5',
          family: 'Claude',
          tier: 3,
          tier_name: 'Opus',
        },
      },
      {provider: 'codex', model: 'gpt-a', default: true},
      {
        provider: 'codex',
        model: 'gpt-x',
        default: false,
        display: {provider_name: 'Codex'},
      },
    );
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.harnesses(LOCAL)).resolves.toEqual({
      kind: 'listed',
      harnesses: [
        {
          provider: 'claude',
          model: 'claude-opus-5-5',
          default: true,
          display: {
            providerName: 'Claude Code',
            modelName: 'Opus 5.5',
            family: 'Claude',
            tier: 3,
            tierName: 'Opus',
          },
        },
        {provider: 'codex', model: 'gpt-a', default: true},
        {provider: 'codex', model: 'gpt-x', default: false, display: {providerName: 'Codex'}},
      ],
    });
  });
});

describe('Hosts.harnesses makers and generations', () => {
  test("carry each pair's maker and generation, asked only of a helper that has them", async () => {
    const link = new FakeLink();
    link.helper.host.harnesses.splice(
      0,
      link.helper.host.harnesses.length,
      ...FAKE_MODEL_CATALOGUE,
    );
    const subject = completionSubject(link);
    await using _manager = subject.manager;
    const listed = async () => {
      const listing = await subject.hosts.harnesses(LOCAL);
      return listing.kind === 'listed' ? listing.harnesses : [];
    };

    const sol = (await listed()).find(offer => 'generation' in (offer.display ?? {}));
    expect(sol?.display).toMatchObject({maker: 'openai', generation: {id: 'gpt-6', order: 1}});
    link.helper.features.delete('harness_generations');
    for (const offer of await listed()) {
      expect(offer.display ?? {}).not.toHaveProperty('maker');
      expect(offer.display ?? {}).not.toHaveProperty('generation');
    }
    const asked = link.helper.requests.filter(request => request.type === 'harnesses.list');
    expect(asked.map(request => 'with_generations' in request)).toEqual([true, false]);
  });
});

describe('Hosts.readEvents', () => {
  test('returns a typed unsupported outcome without sending runs.read_events', async () => {
    const link = new FakeLink();
    link.helper.features.delete('run_history');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.readEvents(LOCAL, '/srv/project', 'run-1', 0, 10)).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'runs.read_events')).toEqual([]);
  });

  test('names the run when the host has no history for it', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.readEvents(LOCAL, '/srv/project', 'run-1', 0, 10)).rejects.toThrow(
      'Could not read the history of run run-1',
    );
  });
});

describe('Hosts.readEvents page size', () => {
  async function asked(features: 'with' | 'without') {
    const link = new FakeLink();
    if (features === 'without') link.helper.features.delete('run_history_pages');
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;
    await subject.hosts.readEvents(LOCAL, '/srv/project', 'run-1', 1, 2000).catch(() => undefined);
    return link.helper.requests.filter(request => request.type === 'runs.read_events');
  }

  test('asks a helper with run_history_pages for whole pages and the journal totals', async () => {
    expect(await asked('with')).toMatchObject([{max: 2000, with_totals: true}]);
  });

  test('asks one without it for the page size it serves and no totals', async () => {
    const [request] = await asked('without');
    expect(request).toMatchObject({max: 500});
    expect(request).not.toHaveProperty('with_totals');
  });
});

describe('Hosts.readSpan', () => {
  test('does not send a span request to a helper without run_span_events', async () => {
    const link = new FakeLink();
    link.helper.features.delete('run_span_events');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(
      subject.hosts.readSpan(LOCAL, '/srv/project', 'run-1', 'op:turn', 1, 100),
    ).resolves.toEqual({kind: 'unsupported'});
    expect(link.helper.requests.filter(request => request.type === 'runs.events_for_span')).toEqual(
      [],
    );
  });

  test('requests only the selected span with a bounded page', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await subject.hosts
      .readSpan(LOCAL, '/srv/project', 'run-1', 'op:turn', 13, 100)
      .catch(() => undefined);
    expect(
      link.helper.requests.filter(request => request.type === 'runs.events_for_span'),
    ).toMatchObject([
      {project: '/srv/project', run_id: 'run-1', span_id: 'op:turn', from_sequence: 13, max: 100},
    ]);
  });
});

describe('Hosts.readSummary', () => {
  const tally = (input: number | null) => ({
    input,
    cache_read: null,
    cache_write: null,
    output: null,
    reasoning: null,
  });
  const record = (execution: string, input: number) => ({
    run_id: 'run-1',
    execution_id: execution,
    span_id: null,
    round_label: null,
    role: 'implementer',
    harness: 'codex',
    model: null,
    reasoning_effort: null,
    harness_version: null,
    started_at: null,
    ended_at: null,
    tokens: tally(input),
    cost_usd: null,
    duration_ms: null,
  });
  const page = (through: number, rest: object) => ({
    through_sequence: through,
    event_count: through,
    journal_bytes: 99,
    trace: 'recorded',
    rounds_finished: 0,
    span_count: 2,
    reply_count: 1,
    usage_count: 1,
    record_count: 2,
    tokens: tally(7),
    overview: null,
    spans: [],
    replies: [],
    schemas: {},
    usage: [],
    records: [],
    next_offset: null,
    ...rest,
  });
  const key = '/srv/project run-1';
  const SCHEMA = 'a'.repeat(64);

  async function subject(
    pages: readonly {offset: number; page: Record<string, unknown>}[],
    features = true,
  ) {
    const link = new FakeLink();
    if (!features) link.helper.features.delete('run_summary');
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.summaries.set(key, pages);
    const built = completionSubject(link);
    return {link, ...built};
  }

  test('joins the pages, each reply with the schema its page or an earlier one carried', async () => {
    const {hosts, manager} = await subject([
      {
        offset: 0,
        page: page(40, {
          spans: [{id: 'a'}, {id: 'b'}],
          next_offset: 2,
          schemas: {[SCHEMA]: {title: 'Plan'}},
          records: [record('inv:a', 3)],
        }),
      },
      {
        offset: 2,
        page: page(40, {
          replies: [{span_id: 'a', kind: 'Plan', schema_id: SCHEMA, value: {summary: 'x'}}],
          usage: [{span_id: 'a', tally: tally(5)}],
          records: [record('inv:b', 4)],
        }),
      },
    ]);
    await using _manager = manager;

    const result = await hosts.readSummary(LOCAL, '/srv/project', 'run-1');

    expect(result).toEqual({
      kind: 'summary',
      summary: {
        throughSequence: 40,
        eventCount: 40,
        journalBytes: 99,
        trace: 'recorded',
        roundsFinished: 0,
        overview: null,
        spans: [{id: 'a'}, {id: 'b'}],
        replies: [{spanId: 'a', kind: 'Plan', jsonSchema: {title: 'Plan'}, value: {summary: 'x'}}],
        tokens: {input: 7, cacheRead: null, cacheWrite: null, output: null, reasoning: null},
        usage: [
          {
            spanId: 'a',
            tokens: {input: 5, cacheRead: null, cacheWrite: null, output: null, reasoning: null},
          },
        ],
        records: [record('inv:a', 3), record('inv:b', 4)],
      },
    });
  });

  test('reads again from the start when the run moved between two pages, and gives up after a few', async () => {
    const moving = await subject([
      {offset: 0, page: page(40, {next_offset: 2})},
      {offset: 2, page: page(41, {})},
    ]);
    await using _manager = moving.manager;

    await expect(moving.hosts.readSummary(LOCAL, '/srv/project', 'run-1')).rejects.toThrow(
      'the run kept changing while its summary was read',
    );
    expect(
      moving.link.helper.requests.filter(request => request.type === 'runs.summary').length,
    ).toBe(6);
  });

  test('is unsupported, and sends nothing, on a helper without run_summary', async () => {
    const {hosts, link, manager} = await subject([{offset: 0, page: page(1, {})}], false);
    await using _manager = manager;

    await expect(hosts.readSummary(LOCAL, '/srv/project', 'run-1')).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'runs.summary')).toEqual([]);
  });

  test('is unsupported, and sends nothing, on a helper whose summary predates its trace', async () => {
    // The pages are what such a helper writes: no `trace` and no `rounds_finished`. Reading one
    // would end the channel, so the machine would look unreachable for good.
    const {trace: _trace, rounds_finished: _rounds, ...before} = page(1, {});
    const {hosts, link, manager} = await subject([{offset: 0, page: before}]);
    link.helper.features.delete('run_trace');
    await using _manager = manager;

    await expect(hosts.readSummary(LOCAL, '/srv/project', 'run-1')).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'runs.summary')).toEqual([]);
    // The machine is still usable for what that helper serves.
    await expect(hosts.projectRuns(LOCAL, '/srv/project')).resolves.toBeDefined();
  });

  test('says the run is gone when the host holds no record of it', async () => {
    const {hosts, manager} = await subject([]);
    await using _manager = manager;

    await expect(hosts.readSummary(LOCAL, '/srv/project', 'run-9')).resolves.toEqual({
      kind: 'gone',
    });
  });
});

describe('Hosts.runDigests', () => {
  const summary = (digest: RunSummary['digest']): RunSummary => ({
    run_id: 'run-1',
    task: 'mpmc',
    display_name: 'run',
    created_at: '2026-10-17T03:00:00Z',
    phase: null,
    instance_id: null,
    ...(digest === undefined ? {} : {digest}),
  });

  test('returns the digest of each named run the host records, with when it began', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.runHistory.set('/srv/project', [
      summary({
        kind: 'digest',
        ideas: 3,
        adopted_ratio: 2.5,
        best_so_far_ratio: null,
        duration_s: 90,
        ending: null,
        closing: 'finished',
      }),
    ]);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    const reply = await subject.hosts.runDigests(LOCAL, '/srv/project', ['run-1']);

    expect(reply.kind).toBe('digests');
    if (reply.kind !== 'digests') return;
    expect(reply.runs.get('run-1')).toEqual({
      kind: 'read',
      createdAt: Date.parse('2026-10-17T03:00:00Z'),
      ideas: 3,
      adoptedRatio: 2.5,
      bestSoFarRatio: null,
      durationSeconds: 90,
      outcome: null,
      closing: 'finished',
      over: true,
      endSummary: null,
      dashboard: null,
    });
  });

  test('asks for the tile data of a helper that offers it, and an older helper is never sent the field', async () => {
    const dashboard = {
      points: [
        {ratio: 1.4, verdict: 'kept' as const, at: '2026-10-17T03:30:00Z'},
        {ratio: null, verdict: 'failed' as const},
      ],
      points_omitted: 2,
      failed_streak: 1,
      last_event_at: '2026-10-17T04:00:00Z',
    };
    const digest = {
      kind: 'digest' as const,
      ideas: 4,
      adopted_ratio: null,
      best_so_far_ratio: 1.4,
      duration_s: 90,
      ending: null,
      closing: null,
      dashboard,
    };
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.runHistory.set('/srv/project', [{...summary(digest), attention: 'stalled'}]);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    const reply = await subject.hosts.runDigests(LOCAL, '/srv/project', ['run-1']);

    expect(link.helper.requests.at(-1)).toMatchObject({type: 'runs.list', with_dashboard: true});
    const entry = reply.kind === 'digests' ? reply.runs.get('run-1') : undefined;
    expect(entry).toMatchObject({
      kind: 'read',
      dashboard: {
        points: [
          {ratio: 1.4, verdict: 'kept', at: Date.parse('2026-10-17T03:30:00Z')},
          {ratio: null, verdict: 'failed', at: null},
        ],
        pointsOmitted: 2,
        failedStreak: 1,
        lastEventAt: Date.parse('2026-10-17T04:00:00Z'),
        attention: 'stalled',
      },
    });

    link.helper.features.delete('run_dashboard');
    const older = await subject.hosts.runDigests(LOCAL, '/srv/project', ['run-1']);
    expect(link.helper.requests.at(-1)).not.toHaveProperty('with_dashboard');
    expect(older.kind === 'digests' && older.runs.get('run-1')).toMatchObject({dashboard: null});
  });

  test('asks for the named runs only, and a run not named carries no digest', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.runHistory.set('/srv/project', [
      summary({kind: 'unreadable', message: 'could not read'}),
    ]);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    const none = await subject.hosts.runDigests(LOCAL, '/srv/project', []);
    expect(none.kind === 'digests' && none.runs.size).toBe(0);
    const named = await subject.hosts.runDigests(LOCAL, '/srv/project', ['run-1']);
    expect(named.kind === 'digests' && named.runs.get('run-1')?.kind).toBe('unreadable');
  });

  test('returns a typed unsupported outcome without sending the request', async () => {
    const link = new FakeLink();
    link.helper.features.delete('run_digest');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.runDigests(LOCAL, '/srv/project', ['run-1'])).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'runs.list')).toEqual([]);
  });
});

describe('Hosts.usageWindow', () => {
  const SINCE = Date.parse('2026-10-17T00:00:00Z');
  const UNTIL = Date.parse('2026-10-18T00:00:00Z');
  const figures = {
    spent_usd: 12.5,
    spend_per_hour_usd: 0.75,
    tokens: {input: 900, cache_read: null, cache_write: null, output: 40, reasoning: null},
    turns: 7,
    unpriced_turns: 1,
    untimed_turns: 0,
    new_bests: 5,
    cost_per_new_best_usd: 2.5,
    by_run: [{run_id: 'run-1', task: 'mpmc', cost_usd: 12.5}],
    by_agent: [{role: 'implementer', harness: 'codex', model: null, cost_usd: 12.5, turns: 7}],
    agents_omitted: 0,
  };
  const listed: RunSummary = {
    run_id: 'run-1',
    task: 'mpmc',
    display_name: 'run',
    created_at: '2026-10-17T03:00:00Z',
    phase: null,
    instance_id: null,
  };

  test('asks for the window in ISO time and returns the figures in the app terms', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.runHistory.set('/srv/project', [listed]);
    link.helper.host.usage.set('/srv/project', figures);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    const reply = await subject.hosts.usageWindow(
      LOCAL,
      '/srv/project',
      ['run-1', 'run-9'],
      SINCE,
      UNTIL,
    );

    expect(link.helper.requests.at(-1)).toMatchObject({
      type: 'usage.window',
      run_ids: ['run-1', 'run-9'],
      since: '2026-10-17T00:00:00.000Z',
      until: '2026-10-18T00:00:00.000Z',
    });
    expect(reply).toEqual({
      kind: 'usage',
      usage: {
        since: SINCE,
        until: UNTIL,
        spentUsd: 12.5,
        spendPerHourUsd: 0.75,
        tokens: figures.tokens,
        turns: 7,
        unpricedTurns: 1,
        untimedTurns: 0,
        newBests: 5,
        costPerNewBestUsd: 2.5,
        byRun: [{runId: 'run-1', task: 'mpmc', costUsd: 12.5}],
        byAgent: [{role: 'implementer', harness: 'codex', model: null, costUsd: 12.5, turns: 7}],
        agentsOmitted: 0,
        missingRuns: ['run-9'],
      },
    });
  });

  test('returns a typed unsupported outcome without sending the request', async () => {
    const link = new FakeLink();
    link.helper.features.delete('usage_window');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(
      subject.hosts.usageWindow(LOCAL, '/srv/project', [], SINCE, UNTIL),
    ).resolves.toEqual({kind: 'unsupported'});
    expect(link.helper.requests.filter(request => request.type === 'usage.window')).toEqual([]);
  });

  test('refuses a window or run list the helper would refuse, without sending it', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;
    const hours = (count: number) => SINCE + count * 3_600_000;

    for (const [ids, since, until] of [
      [[], UNTIL, SINCE],
      [[], SINCE, SINCE],
      [[], SINCE, hours(337)],
      [Array.from({length: 101}, (_, at) => `run-${at}`), SINCE, UNTIL],
      [[], Number.NaN, UNTIL],
    ] as const) {
      await expect(
        subject.hosts.usageWindow(LOCAL, '/srv/project', ids, since, until),
      ).rejects.toThrow();
    }
    await expect(
      subject.hosts.usageWindow(LOCAL, '/srv/project', [], SINCE, hours(336)),
    ).resolves.toMatchObject({kind: 'usage'});
    expect(link.helper.requests.filter(request => request.type === 'usage.window')).toHaveLength(1);
  });

  test('a project the host does not have is an error, not an empty answer', async () => {
    const link = new FakeLink();
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(
      subject.hosts.usageWindow(LOCAL, '/srv/nowhere', [], SINCE, UNTIL),
    ).rejects.toThrow();
  });
});

describe('Hosts.projectRuns', () => {
  const run = (id: string, task: string | null, created: string): RunSummary => ({
    run_id: id,
    task,
    display_name: `name-of-${id}`,
    created_at: created,
    phase: null,
    instance_id: null,
  });

  test('lists every run of the project, newest first, with no digest read', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.runHistory.set('/srv/project', [
      run('run-1', 'mpmc', '2026-10-17T03:00:00Z'),
      run('run-3', null, '2026-10-17T05:00:00Z'),
      run('run-2', 'mpmc', '2026-10-17T04:00:00Z'),
    ]);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    const runs = await subject.hosts.projectRuns(LOCAL, '/srv/project');

    expect(runs).toEqual([
      {
        runId: 'run-3',
        name: 'name-of-run-3',
        task: null,
        createdAt: Date.parse('2026-10-17T05:00:00Z'),
      },
      {
        runId: 'run-2',
        name: 'name-of-run-2',
        task: 'mpmc',
        createdAt: Date.parse('2026-10-17T04:00:00Z'),
      },
      {
        runId: 'run-1',
        name: 'name-of-run-1',
        task: 'mpmc',
        createdAt: Date.parse('2026-10-17T03:00:00Z'),
      },
    ]);
    expect(link.helper.requests.filter(request => request.type === 'runs.list')).toEqual([
      expect.not.objectContaining({digest_runs: expect.anything()}),
    ]);
  });

  test('names the project when the host cannot list its runs', async () => {
    const subject = completionSubject(new FakeLink());
    await using _manager = subject.manager;

    const failure = await subject.hosts.projectRuns(LOCAL, '/srv/missing').then(
      () => null,
      (error: Error) => error,
    );
    expect(failure?.message).toStartWith('Could not list the runs of the project');
    expect(failure?.message).not.toContain('/srv/missing');
  });
});

describe('Hosts checkout changes', () => {
  test('removing a host fences an older checkout validation before acknowledging removal', async () => {
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem: new FakeSettingsFilesystem(),
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    const held = link.deferEstablish();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const change = {owner: '10:100', sequence: 1};
    const changing = hosts.setCheckout(ALPHA, '/new', change);
    await held.received;

    const removing = hosts.remove(ALPHA);
    await expect(
      hosts.setCheckout(ALPHA, '/newer', {owner: '10:100', sequence: 2}),
    ).rejects.toThrow('alpha is being removed');
    held.release();

    await expect(changing).rejects.toThrow('checkout change was cancelled');
    await removing;
    await hosts.cancelCheckout(change);
    expect(settingsFor((await settings.hosts()).settings, ALPHA)).toBeNull();
  });

  test('removing a host wins after an older checkout has crossed its commit boundary', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = filesystem.deferNextReplaceAfterCommit();
    const changing = hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    await held.committed;

    const removing = hosts.remove(ALPHA);
    held.release();

    await expect(changing).resolves.toBe('/src/vibesys');
    await removing;
    expect(settingsFor((await settings.hosts()).settings, ALPHA)).toBeNull();
    expect(manager.existing('ssh:alpha')).toBeNull();
  });

  test('an ambiguous removal write still retires the connection matching its visible result', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const retired: string[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {
        throw new Error('log failed');
      },
      push: () => {},
      retire: key => {
        retired.push(key);
        throw new Error('retire failed');
      },
    });
    await hosts.recent([ALPHA]);
    expect(manager.existing('ssh:alpha')).not.toBeNull();
    const persistence = new Error('directory sync failed');
    filesystem.failNextDirectorySync('/settings/hosts.json', persistence);

    await expect(hosts.remove(ALPHA)).rejects.toBe(persistence);

    expect(settingsFor((await settings.hosts()).settings, ALPHA)).toBeNull();
    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(retired).toEqual(['ssh:alpha']);
  });

  test('a failed removal preserves its persistence error when repair diagnostics also fail', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {
        throw new Error('diagnostic failed');
      },
      push: () => {},
      retire: () => {},
    });
    await hosts.recent([ALPHA]);
    await manager[Symbol.asyncDispose]();
    const persistence = new Error('replace failed');
    filesystem.failNextReplace('/settings/hosts.json', persistence);

    await expect(hosts.remove(ALPHA)).rejects.toBe(persistence);

    expect(settingsFor((await settings.hosts()).settings, ALPHA)?.checkout).toBe('/old');
  });

  test('cancelling checkout validation cannot persist or replace a newer operation', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current =>
      withHostSettings(current, LOCAL, {checkout: '/existing'}),
    );
    const link = new FakeLink();
    const clock = new FakeClock();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: clock.scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(LOCAL, '10:100');
    await manager.existing('local')?.ready();

    const held = filesystem.deferNextWrite();
    const obsolete = hosts.setCheckout(LOCAL, '/cancelled', {owner: '10:100', sequence: 1});
    await held.received;

    // A reloaded renderer may reuse sequence 1, but its document owner keeps the operation unique.
    const newer = hosts.setCheckout(LOCAL, '/newer', {owner: '11:101', sequence: 1});
    const retired = hosts.cancelCheckoutOwner('10:100');
    held.release();
    await expect(obsolete).rejects.toThrow('checkout change was cancelled');
    await retired;
    await expect(newer).resolves.toBe('/src/vibesys');

    expect(settingsFor((await settings.hosts()).settings, LOCAL)?.checkout).toBe('/newer');
    // `/cancelled` opened only its disposable candidate; it never replaced the watched connection.
    expect(link.helper.checkouts).toEqual(['/existing', '/cancelled', '/newer', '/newer']);
    expect(manager.existing('local')?.checkoutRoot).toBe('/src/vibesys');
  });

  test('a cancellation after atomic replace reports and installs the committed checkout', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current =>
      withHostSettings(current, LOCAL, {checkout: '/existing'}),
    );
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(LOCAL, '10:100');
    await manager.existing('local')?.ready();

    const held = filesystem.deferNextReplaceAfterCommit();
    const change = {owner: '10:100', sequence: 1};
    const changing = hosts.setCheckout(LOCAL, '/new', change);
    await held.committed;
    await hosts.cancelCheckout(change);
    held.release();

    await expect(changing).resolves.toBe('/src/vibesys');
    expect(settingsFor((await settings.hosts()).settings, LOCAL)?.checkout).toBe('/new');
    expect(link.helper.checkouts).toEqual(['/existing', '/new', '/new']);
    expect(manager.existing('local')?.checkoutRoot).toBe('/src/vibesys');
  });
});

describe('Hosts desired set', () => {
  test('a checkout read captured before commit cannot acquire the stale checkout afterward', async () => {
    const filesystem = new CapturedReadFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new DeferredAcquireManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const held = filesystem.deferNextRead();
    const acquiring = hosts.connection(ALPHA);
    await held.captured;
    await hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    expect(manager.existing('ssh:alpha')?.where).toBe('/new on fake');
    held.release();

    const lease = await acquiring;
    expect(lease.connection.where).toBe('/new on fake');
    expect(manager.existing('ssh:alpha')).toBe(lease.connection);
    expect(manager.releasedCheckouts).toEqual(['/old']);
    await lease[Symbol.asyncDispose]();
    expect(manager.existing('ssh:alpha')?.where).toBe('/new on fake');
  });

  test.each([
    ['recent reconciliation', (hosts: Hosts) => hosts.recent([ALPHA])],
    ['visible-host reconciliation', (hosts: Hosts) => hosts.host(ALPHA, '10:100')],
  ] as const)('a checkout captured by %s cannot replace a newer same-host commit', async (_kind, reconcile) => {
    const filesystem = new CapturedReadFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });

    const held = filesystem.deferNextRead();
    const older = reconcile(hosts);
    await held.captured;
    await hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    held.release();
    await older;

    expect(manager.existing('ssh:alpha')?.where).toBe('/new on fake');
    expect(link.helper.checkouts).toEqual(['/new', '/new']);
  });

  test.each([
    'before',
    'after',
  ] as const)('an acquisition superseded %s manager delivery disposes once and retries current', async position => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new DeferredAcquireManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const held = manager.deferNext('/old', position);
    const acquiring = hosts.connection(ALPHA);
    await held.received;
    await hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    held.release();

    const lease = await acquiring;
    expect(lease.connection.where).toBe('/new on fake');
    expect(manager.existing('ssh:alpha')).toBe(lease.connection);
    const releasedBeforeReturn =
      position === 'before' ? ['/old', '/old', '/new'] : ['/old', '/old'];
    expect(manager.releasedCheckouts).toEqual(releasedBeforeReturn);
    await lease[Symbol.asyncDispose]();
    await lease[Symbol.asyncDispose]();
    expect(manager.releasedCheckouts).toEqual([...releasedBeforeReturn, '/new']);
    expect(manager.existing('ssh:alpha')?.where).toBe('/new on fake');
  });

  test('a superseded acquisition repairs its host without waiting for an unrelated host', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, BETA, {checkout: '/beta'}));
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    await using manager = new DeferredAcquireManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.recent([BETA, ALPHA]);

    const heldAlpha = manager.deferNext('/old');
    const acquiringAlpha = hosts.connection(ALPHA);
    await heldAlpha.received;
    await hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    const displacedBeta = await manager.acquire(BETA, '/other');
    await displacedBeta[Symbol.asyncDispose]();
    expect(manager.existing('ssh:beta')).toBeNull();
    const heldBeta = manager.deferNext('/beta', 'before');

    heldAlpha.release();
    const winner = await Promise.race([
      acquiringAlpha.then(() => 'alpha' as const),
      heldBeta.received.then(() => 'beta' as const),
    ]);
    heldBeta.release();
    const lease = await acquiringAlpha;

    expect(winner).toBe('alpha');
    expect(lease.connection.where).toBe('/new on fake');
    expect(manager.existing('ssh:alpha')).toBe(lease.connection);
    expect(manager.releasedCheckouts.filter(checkout => checkout === '/old')).toHaveLength(2);
    await lease[Symbol.asyncDispose]();
    expect(manager.existing('ssh:alpha')?.where).toBe('/new on fake');
  });

  test('a non-desired checkout commit cannot invalidate another host reconciliation', async () => {
    const filesystem = new CapturedReadFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/alpha'}));
    await settings.updateHosts(current => withHostSettings(current, BETA, {checkout: '/old-beta'}));
    await using manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });

    const heldAlpha = filesystem.deferNextRead();
    const reconcilingAlpha = hosts.recent([ALPHA]);
    await heldAlpha.captured;
    await hosts.setCheckout(BETA, '/new-beta', {owner: 'retired', sequence: 1});
    expect(manager.existing('ssh:beta')).toBeNull();
    heldAlpha.release();
    await reconcilingAlpha;

    expect(manager.existing('ssh:alpha')?.where).toBe('/alpha on fake');
    expect(settingsFor((await settings.hosts()).settings, BETA)?.checkout).toBe('/new-beta');
  });

  test.each([
    ['recent hosts', (hosts: Hosts) => hosts.recent([ALPHA])],
    ['visible owner', (hosts: Hosts) => hosts.host(ALPHA, '11:101').then(() => undefined)],
  ] as const)('a committed checkout refresh survives an unrelated %s update', async (_update, changeDesired) => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new DeferredAcquireManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const held = manager.deferNext('/new');
    const changing = hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    await held.received;
    await changeDesired(hosts);
    held.release();
    await changing;

    expect(settingsFor((await settings.hosts()).settings, ALPHA)?.checkout).toBe('/new');
    expect(manager.existing('ssh:alpha')?.where).toBe('/new on fake');
    expect(link.helper.checkouts).toEqual(['/old', '/new', '/new']);
  });

  test('a newer committed checkout supersedes an older delayed refresh for the same host', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new DeferredAcquireManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const held = manager.deferNext('/first');
    const first = hosts.setCheckout(ALPHA, '/first', {owner: '10:100', sequence: 1});
    await held.received;
    await hosts.setCheckout(ALPHA, '/second', {owner: '10:100', sequence: 2});
    held.release();
    await first;

    expect(settingsFor((await settings.hosts()).settings, ALPHA)?.checkout).toBe('/second');
    expect(manager.existing('ssh:alpha')?.where).toBe('/second on fake');
    expect(manager.releasedCheckouts).toEqual(['/old', '/first']);
    expect(link.helper.checkouts.at(-1)).toBe('/second');
  });

  test('removing a host revokes and disposes its delayed checkout refresh', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new DeferredAcquireManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const retired: string[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: host => retired.push(host),
    });
    await hosts.host(ALPHA, '10:100');

    const held = manager.deferNext('/new');
    const changing = hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    await held.received;
    await hosts.retireOwner('10:100');
    held.release();
    await changing;

    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(retired).toEqual(['ssh:alpha']);
    expect(manager.releasedCheckouts).toEqual(['/old', '/new']);
  });

  test('reconciliation replaces a watcher whose checkout differs from authoritative settings', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');
    await settings.updateHosts(current =>
      withHostSettings(current, ALPHA, {checkout: '/authoritative'}),
    );

    await hosts.recent([ALPHA]);

    expect(manager.existing('ssh:alpha')?.where).toBe('/authoritative on fake');
    expect(link.helper.checkouts).toEqual(['/old', '/authoritative']);
  });

  test('reconciliation repairs a watcher displaced from the manager shown slot', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');
    const displaced = await manager.acquire(ALPHA, '/other');
    await displaced[Symbol.asyncDispose]();
    expect(manager.existing('ssh:alpha')).toBeNull();

    await hosts.recent([ALPHA]);

    expect(manager.existing('ssh:alpha')?.where).toBe('/old on fake');
    expect(link.helper.checkouts).toEqual(['/old', '/old']);
  });

  test.each([
    'reject',
    'throw',
  ] as const)('an obsolete watched host observes a retirement that %ss exactly once', async mode => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA, BETA]);
    const manager = new FirstReleaseFailureManager(mode, {
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const logged: string[] = [];
    const message = 'could not retire alpha connection: retirement remained unconfirmed';
    let observed = (): void => {};
    const retirementObserved = new Promise<void>(resolve => {
      observed = resolve;
    });
    const retired: string[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: entry => {
        logged.push(entry);
        if (entry === message) observed();
      },
      push: () => {},
      retire: host => retired.push(host),
    });

    await hosts.host(ALPHA, '10:100');
    await manager.existing('ssh:alpha')?.ready();
    await hosts.host(BETA, '10:100');

    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(manager.existing('ssh:beta')).not.toBeNull();
    expect(retired).toEqual(['ssh:alpha']);
    expect(manager.releases).toBe(1);
    await retirementObserved;
    expect(logged).toEqual([message]);
    await manager[Symbol.asyncDispose]();
  });

  test('switching panels releases a host absent from the bounded recent set', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA, BETA]);
    const links = new Map<string, FakeLink>();
    await using manager = new ConnectionManager({
      linkFor: host => {
        const link = new FakeLink();
        links.set(host.kind === 'local' ? 'local' : host.alias, link);
        return link;
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const pushed: HostListing[] = [];
    const retired: string[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: listing => pushed.push(listing),
      retire: host => retired.push(host),
    });

    await hosts.host(ALPHA, '10:100');
    const alphaConnection = manager.existing('ssh:alpha');
    await alphaConnection?.ready();
    await hosts.host(BETA, '10:100');
    await alphaConnection?.[Symbol.asyncDispose]();

    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(manager.existing('ssh:beta')).not.toBeNull();
    expect(links.get('alpha')?.released).toBe(1);
    expect(retired).toEqual(['ssh:alpha']);
    expect(pushed.filter(listing => listing.host === 'ssh:beta').at(0)).toEqual({
      kind: 'unavailable',
      host: 'ssh:beta',
      connection: {kind: 'connecting'},
    });
  });

  test('a connection settled before its host is desired is installed with its real status', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA]);
    const link = new FakeLink();
    link.linkFault = {cause: 'link', detail: 'host unreachable'};
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      // No backoff slot: the one attempt this test drives goes straight to offline.
      config: {delaysMs: [], deadlineMs: 60_000, heartbeatMs: 15_000},
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    });
    const pushed: HostListing[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: listing => pushed.push(listing),
      retire: () => {},
    });

    // `tasks`, `complete`, and `inspect` each lease a host's connection through `Hosts.connection`,
    // independently of whether the welcome view desires that host yet. A connection their lease
    // settles this way, before the host is desired, must not be relabelled once it is installed:
    // offline has no further phase change coming to ever correct a wrong guess.
    const probe = await hosts.connection(ALPHA);
    await expect(probe.connection.ready()).rejects.toThrow('host unreachable');
    expect(probe.connection.phase.kind).toBe('offline');

    await hosts.host(ALPHA, '10:100');

    expect(pushed.filter(listing => listing.host === 'ssh:alpha')).toEqual([
      {
        kind: 'unavailable',
        host: 'ssh:alpha',
        connection: {kind: 'offline', detail: 'host unreachable'},
      },
    ]);
  });

  test('recent and visible intent form one set, independently of update order', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA, BETA]);
    const links = new Map<string, FakeLink>();
    await using manager = new ConnectionManager({
      linkFor: host => {
        const link = new FakeLink();
        links.set(host.kind === 'local' ? 'local' : host.alias, link);
        return link;
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });

    await hosts.host(ALPHA, '10:100');
    const alpha = manager.existing('ssh:alpha');
    await hosts.recent([ALPHA, BETA]);
    await hosts.host(BETA, '10:100');
    expect(manager.existing('ssh:alpha')).toBe(alpha);

    await hosts.recent([]);
    await alpha?.[Symbol.asyncDispose]();
    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(links.get('alpha')?.released).toBe(1);
    expect(manager.existing('ssh:beta')).not.toBeNull();
  });

  test('a late checkout read cannot reinstall an obsolete visible host or listing', async () => {
    const settingsPath = '/settings/hosts.json';
    const filesystem = new DeferredReadFilesystem({
      [settingsPath]: JSON.stringify({
        version: 2,
        hosts: {
          'ssh:alpha': {checkout: '/checkouts/alpha'},
          'ssh:beta': {checkout: '/checkouts/beta'},
        },
      }),
    });
    const settings = new SettingsStore({
      settingsPath,
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    const created: string[] = [];
    await using manager = new ConnectionManager({
      linkFor: host => {
        created.push(host.kind === 'local' ? 'local' : host.alias);
        return new FakeLink();
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const pushed: HostListing[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: listing => pushed.push(listing),
      retire: () => {},
    });

    const obsolete = hosts.host(ALPHA, '10:100');
    await filesystem.received;
    await hosts.host(BETA, '10:100');
    filesystem.release();
    await obsolete;

    expect(created).toEqual(['beta']);
    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(manager.existing('ssh:beta')).not.toBeNull();
    expect(new Set(pushed.map(listing => listing.host))).toEqual(new Set(['ssh:beta']));
    expect(pushed.at(0)).toEqual({
      kind: 'unavailable',
      host: 'ssh:beta',
      connection: {kind: 'connecting'},
    });
  });

  test('checkout refresh finishes the desired set after fencing an older recent read', async () => {
    const settingsPath = '/settings/hosts.json';
    const filesystem = new DeferredReadFilesystem(
      {
        [settingsPath]: JSON.stringify({
          version: 2,
          hosts: {
            'ssh:alpha': {checkout: '/checkouts/alpha'},
            'ssh:beta': {checkout: '/checkouts/beta'},
          },
        }),
      },
      2,
    );
    const settings = new SettingsStore({
      settingsPath,
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await using manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const olderRecent = hosts.recent([ALPHA, BETA]);
    await filesystem.received;
    await hosts.setCheckout(ALPHA, '/checkouts/alpha-new', {owner: '10:100', sequence: 1});

    expect(manager.existing('ssh:alpha')).not.toBeNull();
    expect(manager.existing('ssh:beta')).not.toBeNull();
    filesystem.release();
    await olderRecent;
    expect(manager.existing('ssh:beta')).not.toBeNull();
  });

  test('one host failing to acquire does not abort reconciling the others', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA, BETA]);
    const manager = new ConnectionManager({
      linkFor: () => {
        throw new Error('reconcile must not open a link once the manager is disposed');
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    // The manager disposes (as it does mid-app-shutdown) before either host is acquired, so
    // every acquisition in the pass below rejects the same way a real shutdown race would.
    await manager[Symbol.asyncDispose]();
    const logged: string[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: message => logged.push(message),
      push: () => {},
      retire: () => {},
    });

    // Alpha and beta are both newly desired in this one reconcile pass, alpha attempted first.
    await hosts.recent([ALPHA, BETA]);

    expect(logged).toHaveLength(2);
    expect(logged.some(message => message.includes('alpha'))).toBe(true);
    expect(logged.some(message => message.includes('beta'))).toBe(true);
  });
});

describe('Hosts inspection changes', () => {
  test('a newer completed inspection keeps its locator when the older reply arrives last', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateRecent(current =>
      remember(current, {
        host: 'local',
        project: '/p',
        task: null,
        runId: 'run-1',
        instanceId: null,
        attachedAt: 1,
      }),
    );
    const link = new FakeLink();
    const firstInstance = '000000000001';
    const secondInstance = '000000000002';
    link.helper.publish(record(firstInstance, 'run-1'));
    link.helper.publish(record(secondInstance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');

    const first = hosts.inspect(LOCAL, firstInstance, {owner: '10:100', sequence: 1});
    await held.received;
    await hosts.inspect(LOCAL, secondInstance, {owner: '10:100', sequence: 2});
    held.release();
    await first;

    expect((await settings.recent()).runs[0]?.instanceId).toBe(secondInstance);
  });

  test('retiring a renderer document revokes its pending inspection at the commit boundary', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateRecent(current =>
      remember(current, {
        host: 'local',
        project: '/p',
        task: null,
        runId: 'run-1',
        instanceId: null,
        attachedAt: 1,
      }),
    );
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');

    const inspecting = hosts.inspect(LOCAL, instance, {owner: '10:100', sequence: 1});
    await held.received;
    hosts.retireInspectionOwner('10:100');
    held.release();
    await inspecting;

    expect((await settings.recent()).runs[0]?.instanceId).toBeNull();
    await expect(hosts.inspect(LOCAL, instance, {owner: '10:100', sequence: 2})).rejects.toThrow(
      'inspection owner was retired',
    );
  });

  test('closing an inspector revokes only its pending locator write', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateRecent(current =>
      remember(current, {
        host: 'local',
        project: '/p',
        task: null,
        runId: 'run-1',
        instanceId: null,
        attachedAt: 1,
      }),
    );
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');
    const change = {owner: '10:100', sequence: 1};

    const inspecting = hosts.inspect(LOCAL, instance, change);
    await held.received;
    hosts.cancelInspection(change);
    held.release();
    await inspecting;

    expect((await settings.recent()).runs[0]?.instanceId).toBeNull();
    await hosts.inspect(LOCAL, instance, {owner: change.owner, sequence: 2});
    expect((await settings.recent()).runs[0]?.instanceId).toBe(instance);
  });

  test('an inspection asks the helper for no server log', async () => {
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = inspectorHosts(manager);

    await hosts.inspect(LOCAL, instance, {owner: '10:100', sequence: 1});

    const inspections = link.helper.requests.filter(
      request => request.type === 'instances.inspect',
    );
    expect(inspections).toHaveLength(1);
    expect(inspections[0]).not.toHaveProperty('include_log');
  });

  test("the inspector lists and reads a run's logs over one logs channel, released on close", async () => {
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    link.helper.logStore.open(instance, '000001', {kind: 'run_narration'}, 'vibesys run');
    link.helper.logStore.write(instance, '000001', new TextEncoder().encode('hello\n'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = inspectorHosts(manager);

    const listed = await hosts.logs(LOCAL, instance, {kind: 'list'}, '10:100');
    const read = await hosts.logs(
      LOCAL,
      instance,
      {
        kind: 'read',
        token: 'l1',
        read: {
          instance_id: instance,
          stream_id: '000001',
          start: {tail_bytes: 1024},
          max_bytes: 4096,
          wait_ms: 0,
        },
      },
      '10:100',
    );

    expect(listed?.ok && 'streams' in listed && listed.streams.map(stream => stream.id)).toEqual([
      '000001',
    ]);
    expect(read?.ok && 'chunk' in read && read.chunk.data).toBe('hello\n');
    // The control channel, then the one logs channel both requests shared.
    expect(link.helper.spawned).toBe(2);
    const logsChannel = link.helper.channels[1];
    if (logsChannel === undefined) throw new Error('a logs channel was expected');

    hosts.releaseLogs('10:100');
    await logsChannel.process.exit;
    expect(link.helper.exited).toBeGreaterThanOrEqual(1);
  });

  test("a withdraw aborts the view's read with that token, and withdraws its long poll", async () => {
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    link.helper.logStore.open(instance, '000001', {kind: 'run_narration'}, 'vibesys run');
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = inspectorHosts(manager);
    const owner = '10:100';
    const read = {
      instance_id: instance,
      stream_id: '000001',
      start: {offset: 0},
      max_bytes: 4096,
      wait_ms: 25_000,
    };
    const heldFirst = link.helper.deferNext('logs.read');
    const heldSecond = link.helper.deferNext('logs.read');
    const first = hosts.logs(LOCAL, instance, {kind: 'read', token: 'l1', read}, owner);
    const second = hosts.logs(LOCAL, instance, {kind: 'read', token: 'l2', read}, owner);
    await heldFirst.received;
    await heldSecond.received;

    const withdraw = (token: string, by = owner) =>
      hosts.logs(LOCAL, instance, {kind: 'withdraw', token}, by);
    expect(await withdraw('unknown')).toBeNull();
    expect(await withdraw('l1', '11:100')).toBeNull();
    expect(await withdraw('l1')).toBeNull();
    expect(await first).toMatchObject({ok: false, failure: {code: 'transport'}});
    heldSecond.release();
    expect(await second).toMatchObject({ok: true});
    expect(await withdraw('l2')).toBeNull();

    // The logs channel keeps order: a withdrawal written before this listing reached the helper.
    expect(await hosts.logs(LOCAL, instance, {kind: 'list'}, owner)).toMatchObject({ok: true});
    const [firstRead] = link.helper.requests.filter(request => request.type === 'logs.read');
    const cancels = link.helper.requests.filter(request => request.type === 'logs.cancel');
    expect(cancels).toMatchObject([{read_id: firstRead?.request_id}]);
    heldFirst.release();
    hosts.releaseLogs(owner);
  });
});

function inspectorHosts(manager: ConnectionManager): Hosts {
  return new Hosts({
    manager,
    settings: new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem: new FakeSettingsFilesystem(),
      log: () => {},
    }),
    localCheckout: '/bundled',
    sshConfigPath: '/missing',
    log: () => {},
    push: () => {},
    retire: () => {},
  });
}
