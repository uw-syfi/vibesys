/**
 * Waits until the screen is at rest, so a check reads the layout the app settles on and not a frame
 * of it on the way: the page's fonts are loaded, and the page's own text and the boxes of its
 * elements (to a tenth of a pixel, finer than the stability check's half) are the same on `FRAMES`
 * consecutive animation frames in a row. The wait is a state, not
 * a duration: it ends when the state holds, however fast or slow the machine is. A page that says
 * it is loading (`data-state="loading"`, a reply it asked for still outstanding) is not at rest
 * whatever its frames show: the reply may change them. A spec whose subject is the loading state
 * itself (a slow load, a reconnect, a history held half read) asks for `'as shown'`: the layout at
 * rest in the state the screen says it is in, loading included. `LIMIT` frames is only a guard
 * against a page that never settles, which fails with that said.
 */
import type {Page} from '@playwright/test';

const FRAMES = 6;
const LIMIT = 1200;

/** What `settled` waits for besides stable frames: the page has read what it shows, or not. */
export type Rest = 'read' | 'as shown';

export async function settled(page: Page, rest: Rest = 'read'): Promise<void> {
  const stable = await page.evaluate(
    async ([frames, limit, shown]) => {
      await document.fonts.ready;
      const signature = (): string => {
        const parts: string[] = [document.body.innerText.length.toString()];
        for (const element of document.body.querySelectorAll('*')) {
          const box = element.getBoundingClientRect();
          // To a tenth of a pixel: the stability check holds an element to half a pixel, so a
          // settle that rounds to whole pixels can call a frame at rest that is still moving it.
          const tenths = (value: number): number => Math.round(value * 10);
          parts.push(
            `${tenths(box.x)},${tenths(box.y)},${tenths(box.width)},${tenths(box.height)}`,
          );
        }
        return parts.join('|');
      };
      const frame = (): Promise<void> =>
        new Promise(resolve => requestAnimationFrame(() => resolve()));
      const loading = (): boolean =>
        !shown && document.querySelector('main[data-state="loading"]') !== null;
      let last = '';
      let same = 0;
      for (let seen = 0; seen < limit; seen += 1) {
        await frame();
        const now = signature();
        same = now === last && !loading() ? same + 1 : 0;
        last = now;
        if (same >= frames) return true;
      }
      return false;
    },
    [FRAMES, LIMIT, rest === 'as shown'] as const,
  );
  if (!stable) throw new Error(`the screen did not settle in ${LIMIT} frames`);
}

/**
 * Scrolls the run screen's body to its end, where the Ideas table sits under the charts: its rows
 * are then in view, so a click on one scrolls nothing and a layout check does not read the page
 * moving under a pointer.
 */
export async function scrolledToEnd(page: Page): Promise<void> {
  await page.evaluate(() => {
    const body = document.querySelector('.vs-screen-body');
    body?.scrollTo(0, body.scrollHeight);
  });
}
