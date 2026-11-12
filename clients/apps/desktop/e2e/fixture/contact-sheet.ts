import {writeFile} from 'node:fs/promises';
import type {Page} from '@playwright/test';
import type {FixtureViewport} from './checks.js';

/** One full-size app-window image included in the review sheet. */
export interface FixtureCapture {
  readonly screen: string;
  readonly viewport: FixtureViewport;
  readonly fileName: string;
  readonly png: Buffer;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

/** Draw the captures in-browser, so composing the sheet needs no image utility or new dependency. */
export async function writeContactSheet(
  page: Page,
  captures: readonly FixtureCapture[],
  path: string,
): Promise<void> {
  const figures = captures
    .map(
      capture => `<figure>
        <figcaption>${escapeHtml(capture.screen)} · ${capture.viewport.width}×${capture.viewport.height}</figcaption>
        <img alt="${escapeHtml(capture.screen)} at ${capture.viewport.width} by ${capture.viewport.height}" src="data:image/png;base64,${capture.png.toString('base64')}">
      </figure>`,
    )
    .join('\n');
  await page.setViewportSize({width: 1800, height: 1000});
  await page.setContent(`<!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <title>Desktop fixture contact sheet</title>
        <style>
          * { box-sizing: border-box; }
          html { background: #11151b; color: #f4f6f8; font: 16px/1.4 system-ui, sans-serif; }
          body { margin: 0; padding: 28px; }
          h1 { margin: 0 0 20px; font-size: 24px; }
          .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; }
          figure { margin: 0; padding: 12px; border: 1px solid #46505c; background: #1b222b; }
          figcaption { margin-bottom: 10px; font-weight: 600; }
          img { display: block; width: 100%; height: 360px; object-fit: contain; background: #0a0d11; }
        </style>
      </head>
      <body>
        <h1>VibeSys desktop fixture</h1>
        <div class="grid">${figures}</div>
      </body>
    </html>`);
  await page.evaluate(`Promise.all(Array.from(document.images).map(image =>
    image.complete ? image.decode() : new Promise((resolve, reject) => {
      image.addEventListener('load', () => image.decode().then(resolve, reject), {once: true});
      image.addEventListener('error', reject, {once: true});
    })
  ))`);
  await page.screenshot({path, fullPage: true, animations: 'disabled'});
}

/** Markdown kept beside the images, ready to paste into an artifact or PR description. */
export async function writeCaptureMarkdown(
  captures: readonly FixtureCapture[],
  path: string,
): Promise<void> {
  const fullSize = captures
    .map(
      capture =>
        `- [${capture.screen} · ${capture.viewport.width}×${capture.viewport.height}](./${capture.fileName})`,
    )
    .join('\n');
  await writeFile(
    path,
    `## Desktop fixture screens\n\n![Desktop fixture contact sheet](./contact-sheet.png)\n\nFull-size app-window captures:\n\n${fullSize}\n`,
    'utf8',
  );
}
