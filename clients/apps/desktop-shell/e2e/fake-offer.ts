/**
 * What the Fake helper offers a New run on a machine, shared by every world that opens New run: the
 * machine's description, its vetted harnesses and the bundled examples. A world adds its own
 * `projects` (each project's tasks) to make the `newRun` seed of `FakeBridgeSeed`.
 */
import {exampleCatalogue} from '@vibesys/run-fixtures';
import type {FakeBridgeSeed} from './fake-bridge.js';

type Offer = NonNullable<FakeBridgeSeed['newRun']>;

export const FAKE_OFFER: Pick<Offer, 'machine' | 'harnesses' | 'examples'> = {
  machine: {
    checkout: {root: '/home/me/vibesys', version: '0.9.0', commit: 'cc65df3', dirty: false},
    hardware: {
      cpu_cores: 64,
      gpus: [{vendor: 'amd', model: 'MI300X'}],
      accelerator: 'rocm',
    },
    docker: {reachable: true, version: '27.3.1'},
    harness_clis: ['claude', 'codex'],
    harness_cli_names: {claude: 'Claude Code', codex: 'Codex'},
    runs: {live: 1, ended_today: 2},
  },
  harnesses: [
    {
      provider: 'claude',
      model: 'claude-opus-4-5',
      default: true,
      display: {
        providerName: 'Claude Code',
        modelName: 'Opus 4.5',
        family: 'Claude',
        tier: 3,
        tierName: 'Opus',
        maker: 'anthropic',
      },
    },
    {
      provider: 'claude',
      model: 'claude-sonnet-4-5',
      default: false,
      display: {
        providerName: 'Claude Code',
        modelName: 'Sonnet 4.5',
        family: 'Claude',
        tier: 2,
        tierName: 'Sonnet',
        maker: 'anthropic',
      },
    },
    {
      provider: 'codex',
      model: 'gpt-5-codex',
      default: true,
      display: {providerName: 'Codex', modelName: 'GPT-5 Codex', maker: 'openai'},
    },
  ],
  examples: exampleCatalogue,
};
