import {LocalHost} from './local-host.js';
import {SshHost} from './ssh-host.js';
import type {CommandResult, ServerScript} from './testing/fake-host.js';
import {FakeHost} from './testing/fake-host.js';
import {fakeLocalSystem} from './testing/fake-local-system.js';
import {FakeSsh} from './testing/fake-ssh.js';
import {FakeVibesysNode} from './testing/fake-vibesys-node.js';
import {describeHostContract, type HostWorld} from './testing/host-contract.js';

interface Scripts {
  server: (args: readonly string[]) => ServerScript;
  command: (argv: readonly string[]) => CommandResult;
}

function scripted(make: (scripts: Scripts) => HostWorld['host']): () => HostWorld {
  return () => {
    const scripts: Scripts = {
      server: () => () => {},
      command: () => ({code: 127, stdout: '', stderr: 'not found'}),
    };
    return {
      host: make(scripts),
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
  scripted(
    scripts =>
      new FakeHost({server: args => scripts.server(args), command: argv => scripts.command(argv)}),
  ),
);

describeHostContract(
  'LocalHost',
  scripted(scripts => new LocalHost({python: ['python3'], system: fakeLocalSystem(node(scripts))})),
);

describeHostContract(
  'SshHost',
  scripted(
    scripts =>
      new SshHost({
        alias: 'node-1',
        vibesysCommand: 'vibesys',
        controlPath: '/tmp/vsd/%C',
        askpass: '/app/askpass',
        runner: new FakeSsh(node(scripts)),
      }),
  ),
);
