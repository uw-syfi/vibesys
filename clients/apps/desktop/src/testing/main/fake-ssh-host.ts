/**
 * An ssh host as the main process meets it, over in-memory processes: the master and its control
 * socket (`-O check`, `-O exit`), the commands a channel runs (the platform probe, the checksum,
 * the upload, the gateway's stdio bridge), and the `ps` and `kill` that end a master the host's
 * link left frozen. The gateway channel is bridged to a `FakeGateway`, so the real link pool, ssh
 * link, provisioning and dialers run against it unchanged.
 *
 * A master can be frozen the way a stopped ssh process is: it ignores its control socket (`-O`
 * commands never answer) and every channel through it goes silent, until it is killed by pid.
 */
import type {Duplex} from 'node:stream';
import {gunzipSync} from 'node:zlib';
import {GATEWAY_CONNECT_SCRIPT} from '../../main/gateway-provision.js';
import {PREREQUISITE_SCRIPT} from '../../state/host-prerequisites.js';
import {commandOf} from '../../state/remote-command.js';
import type {FakeGateway} from './fake-gateway.js';
import {type FakeSpawner, fakeSpawner, type Started} from './fake-process.js';

/** What the host and its master have been through. */
interface SshHostState {
  /** A master answers its control socket. */
  masterUp: boolean;
  /** The master is frozen: it answers nothing, and its channels carry nothing. */
  frozen: boolean;
  /** The pid of the master now, or of the last one. */
  pid: number;
  /** Masters started. */
  masters: number;
  /** Pids killed with `kill -KILL`. */
  readonly kills: number[];
  /** Gateway bridges opened: one per dial that reached the host. */
  connects: number;
  /** Platform probes (`uname`): one per provisioning check. */
  probes: number;
  /** Uploads of the host program that completed. */
  uploads: number;
  /** The host holds the program. */
  provisioned: boolean;
  /** Prerequisites the host lacks, as `HOST_PREREQUISITES` names them. */
  readonly lacking: Set<string>;
  /** Prerequisite checks the host answered. */
  preflights: number;
}

export interface FakeSshHost {
  readonly spawner: FakeSpawner;
  readonly state: SshHostState;
  /** Freeze the master: its channels go silent, and its control socket answers nothing. */
  freeze(): void;
}

interface Channel {
  readonly started: Started;
  readonly duplex: Duplex;
}

const scriptOf = (started: Started): string => commandOf(started.args.at(-1) ?? '') ?? '';
const has = (started: Started, word: string): boolean => started.args.includes(word);

/** `peer` answers what the gateway's bridge carries; `platform` is what `uname` says. */
export function fakeSshHost(peer: FakeGateway, platform: string): FakeSshHost {
  const state: SshHostState = {
    masterUp: false,
    frozen: false,
    pid: 4000,
    masters: 0,
    kills: [],
    connects: 0,
    probes: 0,
    uploads: 0,
    provisioned: false,
    lacking: new Set(),
    preflights: 0,
  };
  const channels = new Set<Channel>();

  const bridge = (started: Started): void => {
    state.connects += 1;
    const duplex = peer.connect();
    const channel: Channel = {started, duplex};
    channels.add(channel);
    started.process.input.on('data', (chunk: Buffer) => duplex.write(chunk));
    duplex.on('data', (chunk: Buffer) => started.process.stdout.write(chunk));
    duplex.on('close', () => started.process.end(0));
    started.process.input.on('close', () => duplex.destroy());
  };

  const control = (started: Started): void => {
    if (state.frozen) return;
    if (has(started, 'exit')) {
      state.masterUp = false;
      started.process.end(0);
    } else if (state.masterUp) started.process.end(0, '', `Master running (pid=${state.pid})\r\n`);
    else started.process.end(255, '', 'Control socket connect: No such file or directory');
  };

  const channel = (started: Started): void => {
    const script = scriptOf(started);
    const {process} = started;
    if (script === 'uname -s; uname -m') {
      state.probes += 1;
      return void process.end(0, state.lacking.has('uname') ? '' : platform);
    }
    if (script === PREREQUISITE_SCRIPT) {
      state.preflights += 1;
      return void process.end(0, [...state.lacking].map(name => `missing ${name}\n`).join(''));
    }
    if (script === GATEWAY_CONNECT_SCRIPT) return void bridge(started);
    if (script.includes('gzip -dc')) return void install(started);
    process.end(0, state.provisioned ? 'match' : '');
  };

  /** The install: it takes the compressed program on its input, or fails if the host lacks a tool. */
  const install = ({process}: Started): void => {
    // A host that lacks a tool the install runs fails it with an exit code, as a real one does.
    if (state.lacking.size > 0) return void process.end(1, '', 'sh: command not found');
    const chunks: Buffer[] = [];
    process.input.on('data', (chunk: Buffer) => chunks.push(chunk));
    process.input.on('end', () => {
      gunzipSync(Buffer.concat(chunks));
      state.uploads += 1;
      state.provisioned = true;
      process.end(0);
    });
  };

  /** `ps` and `kill`, which look at and end the master by its pid. */
  const processTool = (started: Started): void => {
    if (started.command === 'ps') {
      const alive = state.masterUp && has(started, String(state.pid));
      return void started.process.end(alive ? 0 : 1, alive ? 'ssh: /tmp/vsd-1/a1b2 [mux]\n' : '');
    }
    const pid = Number(started.args.at(-1));
    if (pid !== state.pid || !state.masterUp) return void started.process.end(1);
    state.kills.push(pid);
    state.masterUp = false;
    state.frozen = false;
    // Every channel through the master dies with it.
    for (const open of channels) open.started.process.end(null);
    started.process.end(0);
  };

  /** `ssh`: a control command, starting the master, or a channel over it. */
  const ssh = (started: Started): void => {
    if (has(started, '-O')) return void control(started);
    if (!has(started, 'ControlMaster=auto')) return void channel(started);
    if (state.frozen) return;
    state.masterUp = true;
    state.pid += 1;
    state.masters += 1;
    started.process.end(0);
  };

  const spawner = fakeSpawner(started =>
    started.command === 'ssh' ? ssh(started) : processTool(started),
  );

  return {
    spawner,
    state,
    freeze: () => {
      state.frozen = true;
      peer.hang();
    },
  };
}
