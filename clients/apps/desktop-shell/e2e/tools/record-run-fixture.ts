/**
 * Records a run's journal as a replayable fixture, with host names, absolute paths and user names
 * removed (`@vibesys/run-fixtures`' recorder). The journal is the run's `run-events.jsonl`, copied
 * from wherever the run was made (the same events `runs.read_events` pages return).
 *
 *   pnpm --filter @vibesys/app-desktop-shell record -- \
 *     --journal run-events.jsonl --name my-run --description "What this run shows" \
 *     --state finished --out libs/run-fixtures/src/data/my-run.json \
 *     [--user NAME]... [--host NAME]... [--objective TEXT --metric NAME --unit UNIT --direction max|min]
 *
 * The recording machine's own user and host names are always scrubbed; `--user` and `--host` add
 * the names of the machine the run was made on. The command refuses to write a fixture in which a
 * leak survives.
 */
import {readFileSync, writeFileSync} from 'node:fs';
import {hostname, userInfo} from 'node:os';
import {parseArgs} from 'node:util';
import {END_OUTCOMES, recordFixture} from '@vibesys/run-fixtures';

const {values} = parseArgs({
  options: {
    journal: {type: 'string'},
    name: {type: 'string'},
    description: {type: 'string'},
    state: {type: 'string', default: 'finished'},
    out: {type: 'string'},
    user: {type: 'string', multiple: true, default: []},
    host: {type: 'string', multiple: true, default: []},
    objective: {type: 'string', default: 'Improve the benchmark.'},
    metric: {type: 'string', default: 'metric'},
    unit: {type: 'string', default: 'units'},
    direction: {type: 'string', default: 'max'},
    outcome: {type: 'string', default: 'adopted'},
    reason: {type: 'string', default: 'adopted the best'},
  },
});

function required(key: 'journal' | 'name' | 'description' | 'out'): string {
  const value = values[key];
  if (value === undefined) throw new Error(`--${key} is required`);
  return value;
}

function oneOf<T extends string>(flag: string, value: string, allowed: readonly T[]): T {
  const found = allowed.find(item => item === value);
  if (found === undefined) throw new Error(`--${flag} is one of ${allowed.join(', ')}`);
  return found;
}

const state = oneOf('state', values.state, ['live', 'finished']);
const machineHost = hostname();
const fixture = recordFixture({
  name: required('name'),
  description: required('description'),
  journal: readFileSync(required('journal'), 'utf8'),
  state,
  objective: {
    text: values.objective,
    metric: values.metric,
    unit: values.unit,
    direction: oneOf('direction', values.direction, ['max', 'min']),
  },
  ...(state === 'finished'
    ? {
        end: {
          outcome: oneOf('outcome', values.outcome, END_OUTCOMES),
          reason: values.reason,
        },
      }
    : {}),
  policy: {
    users: [userInfo().username, ...values.user],
    hosts: [machineHost, machineHost.split('.')[0] ?? machineHost, ...values.host],
  },
});
writeFileSync(required('out'), `${JSON.stringify(fixture, null, 2)}\n`);
process.stdout.write(`recorded ${fixture.events.length} events as ${fixture.name}\n`);
