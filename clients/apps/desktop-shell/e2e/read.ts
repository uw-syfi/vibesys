/**
 * How a spec moves between the app's screens and knows each has read what it shows. The rail and
 * the page of every screen say it as `data-state` (`loading` while a reply they asked for is
 * outstanding, `ready` once none is; see `screen-state.ts` in the shell), so a spec waits for that
 * state and not for a duration: acting at first paint races the bridge's replies, which is what
 * `E2E_JITTER_SEED` makes visible. Every press of the rail goes through `showScreen`.
 */
import {expect, type Page} from '@playwright/test';

/** The rail: the app's one navigation. */
export const sidebar = (page: Page) => page.getByRole('navigation', {name: 'VibeSys'});

/** The rail's destinations. */
export type Destination = 'New' | 'Runs' | 'Machines' | 'Usage' | 'Settings';

/** Returns once the rail and the page say they have read what they show. */
export async function whenRead(page: Page): Promise<void> {
  await expect(sidebar(page)).toHaveAttribute('data-state', 'ready');
  await expect(page.getByRole('main')).toHaveAttribute('data-state', 'ready');
}

/** Presses `destination` in the rail, as a user does, and returns once that screen has read what it shows. */
export async function showScreen(page: Page, destination: Destination): Promise<void> {
  await sidebar(page)
    .getByRole('button', {name: destination, exact: destination === 'New'})
    .click();
  await whenRead(page);
}
