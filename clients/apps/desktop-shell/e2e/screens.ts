/**
 * Walks the Run screen through its levels on a fixture, so every screen on the way can be checked or
 * photographed: the run (ideas), an idea, a step of it and an item of that step, then the run's
 * other faces, then the scrubber. `walkScreens` hands each screen's name and the page to `visit`;
 * the walk itself records what must hold between screens (the anchors stay put).
 */
import {expect, type Page} from '@playwright/test';
import type {Violation} from './checks/in-page.js';
import {settled} from './checks/settle.js';
import {type Anchor, anchorsOf, RUN_ANCHORS, stabilityViolations} from './checks/stability.js';

/** A screen of the walk: a level, or a face of the run level, or the scrubbed run. */
type Screen =
  | 'run'
  | 'idea'
  | 'step'
  | 'item'
  | 'judge'
  | 'timeline'
  | 'agents'
  | 'usage'
  | 'agent'
  | 'scrubbed';

export interface Journey {
  /** The title of the idea to open, or null for the first row. */
  readonly idea: string | null;
  /** The command of a tool call in that idea's implementer turn; null: go no deeper than the idea. */
  readonly command: string | null;
  /** Whether the idea has a judge turn to open after its implementer's. */
  readonly judge: boolean;
  /** Whether the run is live, so its scrubber can be dragged back and returned to live. */
  readonly live: boolean;
}

/** Looks at a screen: what is shown now, under the name the walk gives it. */
export type Visit = (screen: Screen, page: Page) => Promise<void>;

const source = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const escaped = (text: string): RegExp => new RegExp(source(text));
/** The adopted idea can have a star before its title. */
const row = (title: string): RegExp => new RegExp(`^(?:★ )?${source(title)}`);

class Walk {
  readonly moved: Violation[] = [];
  readonly #page: Page;
  readonly #visit: Visit;
  readonly #anchors: readonly Anchor[];
  readonly #before: Awaited<ReturnType<typeof anchorsOf>>;

  constructor(
    page: Page,
    visit: Visit,
    anchors: readonly Anchor[],
    before: Awaited<ReturnType<typeof anchorsOf>>,
  ) {
    this.#page = page;
    this.#visit = visit;
    this.#anchors = anchors;
    this.#before = before;
  }

  async at(screen: Screen): Promise<void> {
    await settled(this.#page);
    await this.#visit(screen, this.#page);
    this.moved.push(
      ...(await stabilityViolations(this.#page, screen, this.#before, this.#anchors)),
    );
  }
}

async function openIdea(page: Page, idea: string | null): Promise<void> {
  await settled(page);
  const options = page.getByRole('listbox', {name: 'Ideas'}).getByRole('option');
  const other = options.first();
  const target = idea === null ? other : options.filter({hasText: row(idea)}).first();
  // Pointing at a row redraws the chart (it lights the row's chain): on a long run that takes long
  // enough to swallow a click that arrives with the first pointer move, so point first, then press.
  await target.hover();
  await settled(page);
  await target.click();
  await expect(page.getByRole('listbox', {name: /steps$/})).toBeVisible();
}

async function openStep(page: Page, role: RegExp): Promise<void> {
  await settled(page);
  await page
    .getByRole('listbox', {name: /steps$/})
    .getByRole('option', {name: role})
    .first()
    .click();
  await expect(page.getByRole('region', {name: 'Agent turn'})).toBeVisible();
}

/** A tool call of the opened turn, in full. */
async function openItem(page: Page, command: string): Promise<void> {
  await settled(page);
  const turn = page.getByRole('region', {name: 'Agent turn'});
  await turn
    .getByRole('button', {name: escaped(command)})
    .last()
    .click();
  await expect(turn.locator('[aria-current="true"]')).toBeVisible();
}

/** The idea level again, by its crumb (the second), from a step or item. */
async function backToIdea(page: Page): Promise<void> {
  await settled(page);
  await page.getByRole('navigation', {name: 'Zoom'}).getByRole('button').nth(1).click();
  await expect(page.getByRole('listbox', {name: /steps$/})).toBeVisible();
}

/** The run level again, by its crumb, from wherever the walk is. */
async function backToRun(page: Page): Promise<void> {
  await settled(page);
  const close = page.getByRole('button', {name: 'Close idea'});
  if (await close.isVisible()) await close.click();
  else
    await page.getByRole('navigation', {name: 'Zoom'}).getByRole('button', {name: 'Run'}).click();
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toBeVisible();
}

/** The first agent of the Agents face, opened beside the list. */
async function openAgent(page: Page): Promise<void> {
  await settled(page);
  await page.getByRole('listbox', {name: 'Agents'}).getByRole('option').first().click();
  await expect(page.getByRole('listbox', {name: 'Turns'})).toBeVisible();
}

/** Walks every screen of `journey`, calling `visit` on each; returns what moved between screens. */
export async function walkScreens(
  page: Page,
  journey: Journey,
  visit: Visit,
  anchors: readonly Anchor[] = RUN_ANCHORS,
): Promise<Violation[]> {
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await settled(page);
  const walk = new Walk(page, visit, anchors, await anchorsOf(page, anchors));
  await walk.at('run');
  await openIdea(page, journey.idea);
  await walk.at('idea');
  if (journey.command !== null) {
    await openStep(page, /builder/i);
    await walk.at('step');
    await openItem(page, journey.command);
    await walk.at('item');
    if (journey.judge) {
      await backToIdea(page);
      await openStep(page, /reviewer/i);
      await walk.at('judge');
    }
  }
  await backToRun(page);
  const show = page.getByRole('radiogroup', {name: 'Show'});
  const faces = [
    {screen: 'timeline', label: 'Timeline'},
    {screen: 'agents', label: 'Agents'},
    {screen: 'usage', label: 'Usage'},
  ] as const;
  for (const face of faces) {
    await show.getByText(face.label).click();
    await walk.at(face.screen);
  }
  await show.getByText('Agents').click();
  await openAgent(page);
  await walk.at('agent');
  await page.getByRole('navigation', {name: 'Zoom'}).getByRole('button', {name: 'Run'}).click();
  await show.getByText('Ideas').click();
  await page.getByRole('slider', {name: 'Run position'}).focus();
  await page.keyboard.press('Home');
  await walk.at('scrubbed');
  if (journey.live) await page.getByRole('button', {name: 'Back to live'}).click();
  return walk.moved;
}
