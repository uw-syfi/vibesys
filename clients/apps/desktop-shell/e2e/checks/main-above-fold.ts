/**
 * The app frame's one promise at every window width: the page is first on screen. It starts in the
 * window's upper half (never below the navigation, which a stacked layout puts above it) and the
 * window does not scroll sideways. Read from the real layout, like the screen checks.
 */
import type {Page} from '@playwright/test';

/** What is wrong with the frame's layout at the window's size now; empty when nothing is. */
export async function frameProblems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const main = document.querySelector('.vs-app-main');
    if (main === null) return ['the window has no main area'];
    const problems: string[] = [];
    const {top, left, width} = main.getBoundingClientRect();
    if (top >= window.innerHeight / 2) {
      problems.push(
        `the main area starts at y=${Math.round(top)} of a ${window.innerHeight}px window`,
      );
    }
    if (width < window.innerWidth / 2) {
      problems.push(`the main area is ${Math.round(width)}px of a ${window.innerWidth}px window`);
    }
    const doc = document.scrollingElement ?? document.documentElement;
    if (doc.scrollWidth > doc.clientWidth) problems.push('the window scrolls sideways');
    if (left < 0) problems.push('the main area starts left of the window');
    return problems;
  });
}

/** A frame that stacks its navigation above its page, as the app did below 800 px: `frameProblems` must fail on it. */
export const STACKED_FRAME = `<div style="display:flex;flex-wrap:wrap;height:100vh;margin:0">
  <nav style="flex:0 0 264px;height:900px">navigation</nav>
  <div class="vs-app-main" style="flex:999 1 520px;min-width:0">the page</div>
</div>`;
