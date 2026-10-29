/**
 * The recorded-fact screens, app window only: Pause when the epoch recorded a root, no Pause when
 * it did not, the checkout skip on an adoption step, and the word "scripted" on a scripted turn.
 * `SCREENS_DIR` writes each shot.
 */
import {mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {expect, type Page, test} from '@playwright/test';
import {
  AdoptionAttrsSchema,
  AgentClient,
  AgentTurnAttrsSchema,
  Change,
  CheckoutStatus,
  EpochStartedSchema,
  RecordSchema,
  RuntimeType,
  Selection,
  SpanStatus,
} from '@vibesys/proto';
import {fixtureNamed, type StoreRunFixture, storeRunFixture} from '@vibesys/run-fixtures';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

const shots = process.env['SCREENS_DIR'];

type RecordBody = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;

function push(fixture: StoreRunFixture, body: RecordBody): StoreRunFixture {
  const records = [...fixture.records];
  records.push(
    create(RecordSchema, {
      seq: (records.at(-1)?.seq ?? 0n) + 1n,
      epoch: records.at(-1)?.epoch ?? 1,
      at: {seconds: 1700000300n + BigInt(records.length)},
      body,
    }),
  );
  return {...fixture, records};
}

function withFacts(source: StoreRunFixture): StoreRunFixture {
  const adopted = push(source, {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'op:adopt',
        parent: 'idea-0',
        runtimeType: RuntimeType.OPERATION,
        name: 'adopt',
        status: SpanStatus.DONE,
        startedAt: 40,
        endedAt: 41,
        label: {
          kind: 'adoption',
          title: 'Adopt',
          attrs: anyPack(
            AdoptionAttrsSchema,
            create(AdoptionAttrsSchema, {
              selection: Selection.BASELINE,
              checkoutStatus: CheckoutStatus.OPERATOR_CHANGED_CHECKOUT,
            }),
          ),
        },
      },
    },
  });
  return push(adopted, {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'turn-scripted',
        parent: 'idea-0',
        runtimeType: RuntimeType.OPERATION,
        name: 'implementer',
        status: SpanStatus.DONE,
        startedAt: 42,
        endedAt: 43,
        label: {
          kind: 'agent_turn',
          title: 'implementer',
          attrs: anyPack(
            AgentTurnAttrsSchema,
            create(AgentTurnAttrsSchema, {
              agent: 'agent-a',
              role: 'implementer',
              stage: 'implement',
              client: AgentClient.SCRIPTED,
            }),
          ),
        },
      },
    },
  });
}

function withoutRoot(source: StoreRunFixture): StoreRunFixture {
  const records = source.records.map(record => {
    if (record.body.case !== 'epochStarted') return record;
    const value = create(EpochStartedSchema, {...record.body.value, projectRoot: undefined});
    return create(RecordSchema, {
      seq: record.seq,
      epoch: record.epoch,
      at: record.at,
      body: {case: 'epochStarted', value},
    });
  });
  return {...source, records};
}

async function openGateway(page: Page, fixture: StoreRunFixture): Promise<void> {
  await page.setViewportSize({width: 1440, height: 900});
  await installGateway(page, wire(fixture));
  await openFixture(page, {...fixtureNamed('queue-rs-live'), runId: 'run-a'});
  await expect(page.getByRole('heading', {name: 'queue-rs · mpmc'})).toBeVisible();
  await settled(page);
}

async function shot(page: Page, name: string): Promise<void> {
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  await page.screenshot({path: join(shots, `${name}.png`)});
}

test('recorded root shows Pause, the checkout skip, and scripted', async ({page}) => {
  await openGateway(page, withFacts(storeRunFixture('live')));
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toBeVisible();
  await expect(page.getByText('scripted')).toBeVisible();
  await shot(page, 'pause-and-scripted');
  await page.getByRole('option', {name: 'Idea 1'}).click();
  await expect(page.getByText('Operator changed checkout')).toBeVisible();
  await settled(page);
  await shot(page, 'checkout-skip');
});

test('a run whose log records no root is still addressed by the catalog, so it shows Pause', async ({
  page,
}) => {
  await openGateway(page, withoutRoot(storeRunFixture('live')));
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toBeVisible();
  await shot(page, 'no-recorded-root');
});
