/**
 * Waits until the screen is at rest, so a check reads the layout the app settles on and not a frame
 * of it on the way: the page's fonts are loaded, and the page's own text and the boxes of its
 * elements are the same on `FRAMES` consecutive animation frames in a row. The wait is a state, not
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
          parts.push(
            `${Math.round(box.x)},${Math.round(box.y)},${Math.round(box.width)},${Math.round(box.height)}`,
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
