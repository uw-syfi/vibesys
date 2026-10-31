import {mkdirSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {Controls, type Step} from './chat-controls.js';
import {emitChat} from './chat-events.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The run chat's panel on a live run, with no backend: the chat is a session log the fake gateway
 * serves, the sends are answered by it, and what the run does with a steer is appended to the run
 * log as the records the run would write. Each screen passes the layout and
 * wording checks: the panel closed and open at 1440, 1000 and 660 px; a streaming answer; a
 * proposed steer, edited; every state a sent steer can be in; a standing steer with Withdraw; the
 * harness picker; and an empty thread. `SCREENS_DIR=<dir>` writes each screen's screenshot
 * (the page only).
 */

const FIXTURE = 'queue-rs-live';
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

const THREAD = 'thread-a';
const CHAT = {agent_kind: 'chat', round_label: 'experiment-chat', chat_thread_id: THREAD} as const;

/** A wire event of the chat's: the envelope the fake run server streams, with the caller's fields. */
function event(sequence: number, type: string, fields: object = {}): RunEvent {
  return {timestamp: '2026-10-15T16:31:00Z', type, sequence, ...fields} as unknown as RunEvent;
}

function created(sequence: number): RunEvent {
  return event(sequence, 'chat_thread_created', {
    ...CHAT,
    data: {
      kind: 'chat_thread_created',
      thread_id: THREAD,
      title: 'Best idea so far',
      provider: 'claude',
      model: 'opus',
      created_at: '2026-10-15T16:31:00Z',
    },
  });
}

function chunk(sequence: number, content: string): RunEvent {
  return event(sequence, 'agent_output_chunk', {
    ...CHAT,
    invocation_id: `turn-${sequence}`,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  });
}

function answered(sequence: number, question: string, answer: string, turn: number): RunEvent {
  return event(sequence, 'chat', {
    ...CHAT,
    text: question,
    data: {kind: 'chat', answer, invocation_id: `turn-${turn}`},
  });
}

function proposed(sequence: number): RunEvent {
  return event(sequence, 'steer_proposed', {
    chat_thread_id: THREAD,
    data: {
      kind: 'steer_proposed',
      proposal_id: 'proposal-1',
      proposal: {
        text: 'Pad the head and tail counters to their own cache lines.',
        target: {kind: 'role', id: 'implementer'},
        mode: 'standing',
      },
    },
  });
}

const ANSWER = [
  'The best idea so far is the lock-free ring with per-slot sequence numbers, at **1.18×** the input.',
  '',
  '- 48.6 ops/s against 41.2.',
  '- Cache-line padding passed at 1.07×.',
  '- Larger batch size failed at 0.76×.',
].join('\n');

/** The sequence after the fixture's own events: where the chat's events start. */
function firstSequence(): number {
  const events = fixtureNamed(FIXTURE).events as readonly {sequence?: number}[];
  return Math.max(...events.map(each => each.sequence ?? 0)) + 1;
}

/** A conversation: one answered question, a proposal, and a second answer still streaming. */
function conversation(from: number): readonly RunEvent[] {
  return [
    created(from),
    chunk(from + 1, 'The best idea so far is the lock-free ring.'),
    answered(from + 2, 'What is the best idea?', ANSWER, from + 1),
    proposed(from + 3),
  ];
}

async function emit(page: Page, events: readonly RunEvent[]): Promise<void> {
  await emitChat(page, events);
}

async function open(page: Page, width: number): Promise<Controls> {
  await page.setViewportSize({width, height: 900});
  const fixture = fixtureNamed(FIXTURE);
  const controls = new Controls(page, fixture);
  await controls.attach();
  await openFixture(page, fixture);
  await playLive(page, fixture);
  await settled(page);
  expect(await page.evaluate(() => window.innerWidth)).toBe(width);
  return controls;
}

async function openPanel(page: Page): Promise<void> {
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await expect(page.getByRole('region', {name: 'Chat'})).toBeVisible();
  await settled(page);
}

async function check(page: Page, name: string): Promise<void> {
  expect(await screenCheck(page), `${name} passes the layout and wording checks`).toEqual([]);
  if (shots !== undefined) await page.screenshot({path: `${shots}/run-chat__${name}.png`});
}

const panel = (page: Page) => page.getByRole('region', {name: 'Chat'});

async function sendProposal(
  page: Page,
  controls: Controls,
  mode: 'Next turn' | 'Every turn',
): Promise<string> {
  await panel(page).getByRole('radiogroup', {name: 'When'}).getByText(mode, {exact: true}).click();
  await panel(page).getByRole('button', {name: 'Send', exact: true}).click();
  await expect(panel(page).getByText('Sent', {exact: true})).toBeVisible();
  await expect.poll(() => controls.posted.length).toBeGreaterThan(0);
  const id = controls.lastId();
  expect(id).not.toBe('');
  return id;
}

for (const width of [1440, 1000, 660]) {
  test(`the panel closed, then open, at ${width}px`, async ({page}) => {
    await open(page, width);
    await emit(page, conversation(firstSequence()));
    await settled(page);
    await expect(page.getByRole('button', {name: 'Chat', exact: true})).toBeVisible();
    await check(page, `closed-${width}`);
    await openPanel(page);
    await expect(panel(page).getByText('What is the best idea?')).toBeVisible();
    await expect(panel(page).getByRole('button', {name: 'Send', exact: true})).toBeVisible();
    await check(page, `open-${width}`);
    // The run keeps its content: beside the panel, or put away under it where the window is too
    // narrow for both, and back when the panel is closed.
    if (width > 660) await expect(page.getByRole('heading', {level: 1})).toBeVisible();
    await panel(page).getByRole('button', {name: 'Close'}).click();
    await expect(page.getByRole('heading', {level: 1})).toBeVisible();
  });
}

test('the last thread opens by default, with a streaming answer', async ({page}) => {
  await open(page, 1440);
  const from = firstSequence();
  await emit(page, [
    created(from),
    chunk(
      from + 1,
      'The best idea so far is the lock-free ring with per-slot sequence numbers, because it',
    ),
  ]);
  await openPanel(page);
  await expect(panel(page).getByText('Writing')).toBeVisible();
  await check(page, 'streaming');
});

test('a proposed steer shows as a card, edited before anything is sent', async ({page}) => {
  const controls = await open(page, 1440);
  await emit(page, conversation(firstSequence()));
  await openPanel(page);
  const text = panel(page).getByRole('textbox', {name: 'Steer text'});
  await expect(text).toHaveValue('Pad the head and tail counters to their own cache lines.');
  await text.fill('Pad head and tail to their own cache lines.');
  // The target is one select: the whole run, then each role of the run by its display name.
  await expect(panel(page).getByRole('combobox', {name: 'To'}).locator('option')).toHaveText([
    'Whole run',
    'Planner',
    'Builder',
    'Reviewer',
  ]);
  await panel(page).getByRole('radiogroup', {name: 'When'}).getByText('Now', {exact: true}).click();
  expect(controls.posted.length).toBe(0);
  await check(page, 'proposal-edited');
});

const DELIVERIES: readonly {
  readonly name: string;
  readonly mode: 'Next turn' | 'Every turn';
  readonly steps: readonly Step[];
  readonly shows: string;
}[] = [
  {name: 'sent', mode: 'Next turn', steps: [], shows: 'Sent'},
  {name: 'waiting', mode: 'Next turn', steps: [{state: 'queued'}], shows: 'Waiting'},
  {
    name: 'reached',
    mode: 'Next turn',
    steps: [{state: 'queued'}, {state: 'reached', role: 'implementer'}],
    shows: 'Reached · Builder',
  },
  {
    name: 'in-force',
    mode: 'Every turn',
    steps: [{state: 'queued'}, {state: 'in_force', role: 'implementer'}],
    shows: 'In force · Builder · every turn',
  },
  {
    name: 'dropped',
    mode: 'Next turn',
    steps: [{state: 'queued'}, {state: 'dropped'}],
    shows: 'Dropped',
  },
  {
    name: 'ended',
    mode: 'Every turn',
    steps: [{state: 'in_force', role: 'implementer'}, {state: 'ended'}],
    shows: 'Ended',
  },
  {
    name: 'refused',
    mode: 'Next turn',
    steps: [{state: 'refused', reason: 'no such role'}],
    shows: 'Refused: no such role',
  },
];

for (const each of DELIVERIES) {
  test(`a sent steer shows ${each.name}`, async ({page}) => {
    const controls = await open(page, 1440);
    await emit(page, conversation(firstSequence()));
    await openPanel(page);
    const id = await sendProposal(page, controls, each.mode);
    for (const step of each.steps) await controls.deliver(id, step);
    await expect(panel(page).getByText(each.shows, {exact: true})).toBeVisible();
    await settled(page);
    await check(page, `delivery-${each.name}`);
  });
}

test('a standing steer in force offers Withdraw, which ends it', async ({page}) => {
  const controls = await open(page, 1440);
  await emit(page, conversation(firstSequence()));
  await openPanel(page);
  const id = await sendProposal(page, controls, 'Every turn');
  await controls.deliver(id, {state: 'in_force', role: 'implementer'});
  await expect(panel(page).getByRole('button', {name: 'Withdraw'})).toBeVisible();
  await check(page, 'standing-withdraw');
  await panel(page).getByRole('button', {name: 'Withdraw'}).click();
  await expect(panel(page).getByText('Withdrawing', {exact: true})).toBeVisible();
  await expect.poll(() => controls.posted.length).toBe(2);
  await controls.deliver(id, {state: 'ended', reason: 'withdrawn'});
  await expect(panel(page).getByText('Withdrawn', {exact: true})).toBeVisible();
  await check(page, 'standing-withdrawn');
});

test('New steer turns what is typed into a card, with no text left in the composer', async ({
  page,
}) => {
  await open(page, 1440);
  await emit(page, conversation(firstSequence()));
  await openPanel(page);
  const composer = panel(page).getByRole('textbox', {name: 'Message'});
  await composer.fill('Spend the next idea on batching.');
  await panel(page).getByRole('button', {name: 'New steer', exact: true}).click();
  await expect(composer).toHaveValue('');
  await expect(panel(page).getByRole('textbox', {name: 'Steer text'})).toHaveCount(2);
  await check(page, 'steer-written');
});

test('a new chat shows the harness picker and an empty thread', async ({page}) => {
  await open(page, 1440);
  await emit(page, conversation(firstSequence()));
  await openPanel(page);
  await panel(page).getByRole('button', {name: 'New chat'}).click();
  await expect(panel(page).getByRole('textbox', {name: 'Message'})).toHaveAttribute(
    'placeholder',
    'Ask about this run',
  );
  await expect(panel(page).getByRole('combobox', {name: 'Harness'})).toBeVisible();
  await check(page, 'empty-thread-picker');
  const models = panel(page).getByRole('combobox', {name: 'Model'});
  await expect(models).toBeVisible();
  await models.focus();
  await check(page, 'picker-open');
});
