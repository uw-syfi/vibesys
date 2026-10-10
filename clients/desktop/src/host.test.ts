import type {CommandResult, ServerScript} from './fake-host.js';
import {FakeHost} from './fake-host.js';
import {LocalHost} from './local-host.js';
import {fakeLocalSystem} from './testing/fake-local-system.js';
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

describeHostContract(
  'FakeHost',
  scripted(
    scripts =>
      new FakeHost({server: args => scripts.server(args), command: argv => scripts.command(argv)}),
  ),
);

describeHostContract(
  'LocalHost',
  scripted(
    scripts =>
      new LocalHost({
        python: ['python3'],
        system: fakeLocalSystem({
          server: args => scripts.server(args),
          command: argv => scripts.command(argv),
        }),
      }),
  ),
);
