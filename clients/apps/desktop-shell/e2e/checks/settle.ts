/**
 * Waits until the screen is at rest, so a check reads the layout the app settles on and not a frame
 * of it on the way: the page's fonts are loaded, and the page's own text and the boxes of its
 * elements (to a tenth of a pixel, finer than the stability check's half) are the same on `FRAMES`
 * consecutive animation frames in a row. The wait is a state, not
 * a duration: it ends when the state holds, however fast or slow the machine is. `LIMIT` frames is
 * only a guard against a page that never settles, which fails with that said.
 */
import type {Page} from '@playwright/test';

const FRAMES = 6;
const LIMIT = 1200;

export async function settled(page: Page): Promise<void> {
  const stable = await page.evaluate(
    async ([frames, limit]) => {
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
      let last = '';
      let same = 0;
      for (let seen = 0; seen < limit; seen += 1) {
        await frame();
        const now = signature();
        same = now === last ? same + 1 : 0;
        last = now;
        if (same >= frames) return true;
      }
      return false;
    },
    [FRAMES, LIMIT] as const,
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
