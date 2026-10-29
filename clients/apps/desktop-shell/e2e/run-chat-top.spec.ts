import {mkdirSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {emitChat} from './chat-events.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * A chat thread as a real one reads: a long first question, long answers, a thread that already
 * exists when the panel opens. Regression: it opened at the end of its last answer, which cut the
 * question above by however much the answer overflowed the pane (a few lines of a short thread),
 * leaving the first bubble clipped under the header's edge. Now it opens at its latest question,
 * whole, for every length of answer; and scrolled to its start the first message sits whole too,
 * at every width. `SCREENS_DIR=<dir>` writes each screenshot
 * (the page only).
 */

const FIXTURE = 'queue-rs-live';
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

const THREAD = 'thread-top';
const CHAT = {agent_kind: 'chat', round_label: 'experiment-chat', chat_thread_id: THREAD} as const;

function event(sequence: number, type: string, fields: object = {}): RunEvent {
  return {timestamp: '2026-10-15T16:31:00Z', type, sequence, ...fields} as unknown as RunEvent;
}

function created(sequence: number): RunEvent {
  return event(sequence, 'chat_thread_created', {
    ...CHAT,
    data: {
      kind: 'chat_thread_created',
      thread_id: THREAD,
      title: 'Question only, change nothing: how many ideas have passed',
      provider: 'claude',
      model: 'opus',
      created_at: '2026-10-15T16:31:00Z',
    },
  });
}

function answered(sequence: number, question: string, answer: string): RunEvent {
  return event(sequence, 'chat', {
    ...CHAT,
    text: question,
    data: {kind: 'chat', answer, invocation_id: `turn-${sequence}`},
  });
}

const QUESTION =
  'Question only, change nothing: how many ideas have passed, and what is the best measured output tokens per second so far?';
const ANSWER = [
  "I can't give you a measured number. The `vibesys-run` tools failed to connect, so I couldn't query ideas or steps.",
  '',
  '- **Ideas passed:** none that I can find. The run directory under `.vibesys/state/runs/` has no idea, step or result records.',
  '- **Best measured value:** none recorded in the files I could read.',
  '',
  'The only files for this run are the manifest and the journal, and neither names a result.',
].join('\n');

function firstSequence(): number {
  const events = fixtureNamed(FIXTURE).events as readonly {sequence?: number}[];
  return Math.max(...events.map(each => each.sequence ?? 0)) + 1;
}

async function emit(page: Page, events: readonly RunEvent[]): Promise<void> {
  await emitChat(page, events);
}

for (const width of [1440, 1000]) {
  test(`the first message of an existing long thread is whole at the top, at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height: 900});
    const fixture = fixtureNamed(FIXTURE);
    await openFixture(page, fixture);
    await playLive(page, fixture);
    await settled(page);
    const from = firstSequence();
    const turns = Array.from({length: 12}, (_, index) =>
      answered(
        from + 1 + index,
        index === 0 ? QUESTION : `${QUESTION} (${index})`,
        ANSWER.slice(0, 120 + ((index * 97) % (ANSWER.length - 120))),
      ),
    );
    await emit(page, [created(from), ...turns]);
    await page.getByRole('button', {name: 'Chat', exact: true}).click();
    const panel = page.getByRole('region', {name: 'Chat'});
    await expect(panel).toBeVisible();
    await settled(page);
    const thread = panel.getByRole('list', {name: /./}).first();
    // Scroll to the start the way a reader does: the wheel, until the pane stops moving.
    await thread.hover();
    for (let step = 0; step < 60; step += 1) {
      await page.mouse.wheel(0, -120);
      await page.evaluate(
        () =>
          new Promise<void>(done =>
            requestAnimationFrame(() => requestAnimationFrame(() => done())),
          ),
      );
    }
    await settled(page);
    const where = await thread.evaluate(pane => {
      const first = pane.querySelector<HTMLElement>('[aria-posinset="1"]');
      const bubble = first?.querySelector<HTMLElement>('.vs-chat-message-text');
      return {
        scrollTop: pane.scrollTop,
        pane: pane.getBoundingClientRect().top,
        bubble: bubble?.getBoundingClientRect().top ?? null,
      };
    });
    expect(where.scrollTop).toBe(0);
    expect(where.bubble).not.toBeNull();
    // The bubble starts below the pane's top edge: nothing of it is under the header.
    expect(where.bubble ?? 0).toBeGreaterThanOrEqual(where.pane);
    if (shots !== undefined) await page.screenshot({path: `${shots}/run-chat-top__${width}.png`});
  });
}

for (const width of [1440, 1000]) {
  test(`a thread opens at its latest question, whole, for any answer length, at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height: 900});
    const fixture = fixtureNamed(FIXTURE);
    await openFixture(page, fixture);
    await playLive(page, fixture);
    await settled(page);
    const from = firstSequence();
    // The answers cover a thread that fits, one that overflows by a line or two, and a long one.
    const lengths = [150, 700, 800, 900, 1000, 1100, 1300, 2400, 4800];
    const text = Array.from({length: 40}, () => ANSWER).join('\n\n');
    await emit(page, [created(from)]);
    await page.getByRole('button', {name: 'Chat', exact: true}).click();
    const panel = page.getByRole('region', {name: 'Chat'});
    await expect(panel).toBeVisible();
    for (const [index, length] of lengths.entries()) {
      const at = from + 1 + index;
      await emit(page, [answered(at, `${QUESTION} (${index})`, text.slice(0, length))]);
      // Close and open the panel: the thread is already there when it opens.
      await panel.getByRole('button', {name: 'Close'}).click();
      await page.getByRole('button', {name: 'Chat', exact: true}).click();
      await settled(page);
      const thread = panel.getByRole('list', {name: /./}).first();
      const where = await thread.evaluate(pane => {
        const rows = [...pane.querySelectorAll<HTMLElement>('[aria-posinset]')];
        const bubbles = rows.flatMap(row => [
          ...row.querySelectorAll<HTMLElement>('[data-side="end"] > .vs-chat-message-text'),
        ]);
        const last = bubbles.at(-1);
        return {
          pane: pane.getBoundingClientRect().top,
          bubble: last?.getBoundingClientRect().top ?? null,
          text: last?.textContent ?? '',
        };
      });
      expect(where.text, `answer of ${length}`).toContain(`(${index})`);
      expect(where.bubble ?? -1, `answer of ${length}`).toBeGreaterThanOrEqual(where.pane);
      if (shots !== undefined && length === 1100) {
        await page.screenshot({path: `${shots}/run-chat-opens__${width}.png`});
      }
    }
  });
}
