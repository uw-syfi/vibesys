/** Install a world's dependent Fakes in one browser initialization, in declaration order. */
import type {BrowserContext, Page} from '@playwright/test';

export function initScript<T>(install: (seed: T) => void, seed: T): string {
  return `(${install.toString()})(${JSON.stringify(seed)});`;
}

export async function addInitScripts(
  target: Page | BrowserContext,
  sources: readonly string[],
): Promise<void> {
  await target.addInitScript({content: sources.join('\n')});
}
