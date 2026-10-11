import {describe, expect, test} from 'bun:test';
import type {Host} from './host.js';
import {HOST_COMMAND_OUTPUT_LIMIT_BYTES, HostError} from './host.js';
import {LocalHost} from './local-host.js';
import {SshHost} from './ssh-host.js';
import type {CommandResult, ServerScript} from './testing/fake-host.js';
import {FakeHost} from './testing/fake-host.js';
import {fakeLocalSystem} from './testing/fake-local-system.js';
import {fakeProcess} from './testing/fake-process.js';
import {FakeSsh} from './testing/fake-ssh.js';
import {FakeVibesysNode} from './testing/fake-vibesys-node.js';
import {describeHostContract, type HostWorld} from './testing/host-contract.js';

interface Scripts {
  server: (args: readonly string[]) => ServerScript;
  command: (argv: readonly string[]) => CommandResult;
}

interface Made {
  readonly host: HostWorld['host'];
  readonly network: {isListening(path: string): boolean};
}

function scripted(make: (scripts: Scripts) => Made): () => HostWorld {
  return () => {
    const scripts: Scripts = {
      server: () => () => {},
      command: () => ({code: 127, stdout: '', stderr: 'not found'}),
    };
    const made = make(scripts);
    return {
      host: made.host,
      isListening: path => made.network.isListening(path),
      scriptServer: script => {
        scripts.server = script;
      },
      scriptCommand: command => {
        scripts.command = command;
      },
    };
  };
}

function node(scripts: Scripts): FakeVibesysNode {
  return new FakeVibesysNode({
    server: args => scripts.server(args),
    command: argv => scripts.command(argv),
  });
}

describeHostContract(
  'FakeHost',
  scripted(scripts => {
    const host = new FakeHost({
      server: args => scripts.server(args),
      command: argv => scripts.command(argv),
    });
    return {host, network: host.network};
  }),
);

describeHostContract(
  'LocalHost',
  scripted(scripts => {
    const machine = node(scripts);
    const host = new LocalHost({python: ['python3'], system: fakeLocalSystem(machine)});
    return {host, network: machine.network};
  }),
);

describeHostContract(
  'SshHost',
  scripted(scripts => {
    const machine = node(scripts);
    const host = new SshHost({
      alias: 'node-1',
      checkout: '/home/user/src/vibesys',
      controlPath: '/tmp/vsd/%C',
      askpass: '/app/askpass',
      runner: new FakeSsh(machine),
    });
    return {host, network: machine.network};
  }),
);

async function failureOf(promise: Promise<unknown>): Promise<HostError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof HostError) return error;
    throw error;
  }
  throw new Error('expected a HostError');
}

describe('Host command output bound', () => {
  for (const stream of ['stdout', 'stderr'] as const) {
    test(`crossing the ${stream} bound kills the process once and reports a bounded error`, async () => {
      const result: CommandResult = {
        code: 0,
        stdout: stream === 'stdout' ? `"${'x'.repeat(HOST_COMMAND_OUTPUT_LIMIT_BYTES - 1)}"` : '{}',
        stderr: stream === 'stderr' ? 'e'.repeat(HOST_COMMAND_OUTPUT_LIMIT_BYTES + 1) : '',
      };
      const signals: (NodeJS.Signals | number)[] = [];
      const machine = new FakeVibesysNode({server: () => () => {}, command: () => result});
      const system = fakeLocalSystem(machine, output => {
        const child = fakeProcess(signal => signals.push(signal));
        child.stdout.write(output.stdout);
        child.stderr.write(output.stderr);
        return child.process;
      });
      const host: Host = new LocalHost({python: ['python3'], system});

      const error = await failureOf(host.invoke(['large-output']));
      expect(error.kind).toBe('failed');
      expect(error.message).toContain(`${HOST_COMMAND_OUTPUT_LIMIT_BYTES} bytes to ${stream}`);
      expect(error.message.length).toBeLessThan(200);
      expect(signals).toEqual(['SIGKILL']);
      await host.close();
    });
  }
});
