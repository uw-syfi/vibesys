import {expect, test} from '@playwright/test';

const THEME_KEY = 'vibesys.web.theme';

test('keyboard selection applies immediately, survives reload, and resets explicitly', async ({
  page,
}) => {
  await page.goto('/');
  const picker = page.getByRole('combobox', {name: 'Theme'});
  await expect(picker).toHaveValue('dark');

  await picker.focus();
  await picker.press('ArrowDown');

  await expect(picker).toHaveValue('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(page.getByText('Browser override. Host default: Dark.')).toBeVisible();
  expect(await page.evaluate(key => localStorage.getItem(key), THEME_KEY)).toBe('light');

  await page.reload();
  await expect(page.getByRole('combobox', {name: 'Theme'})).toHaveValue('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

  const reset = page.getByRole('button', {name: 'Use host default'});
  await reset.focus();
  await reset.press('Enter');
  await expect(picker).toHaveValue('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByText('Using host default: Dark.')).toBeVisible();
  expect(await page.evaluate(key => localStorage.getItem(key), THEME_KEY)).toBeNull();
});

test('removes an obsolete persisted theme instead of treating it as an override', async ({
  page,
}) => {
  await page.addInitScript(({key}) => localStorage.setItem(key, 'theme-that-no-longer-exists'), {
    key: THEME_KEY,
  });
  await page.goto('/');

  await expect(page.getByRole('combobox', {name: 'Theme'})).toHaveValue('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByText('Using host default: Dark.')).toBeVisible();
  expect(await page.evaluate(key => localStorage.getItem(key), THEME_KEY)).toBeNull();
});

test('a light theme reaches the complete gateway connection surface through root tokens', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByRole('combobox', {name: 'Theme'}).selectOption('light');

  const themedStyles = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const input = document.querySelector<HTMLInputElement>('#gateway-url');
    const button = document.querySelector<HTMLButtonElement>('.gateway-connect button');
    const status = document.querySelector<HTMLElement>('.gateway-connect-status');
    if (input === null || button === null || status === null)
      throw new Error('gateway form missing');
    const normalized = (value: string): string => {
      const probe = document.createElement('span');
      probe.style.color = value;
      document.body.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return color;
    };
    const inputStyle = getComputedStyle(input);
    const buttonStyle = getComputedStyle(button);
    const statusStyle = getComputedStyle(status);
    return {
      root: {
        field: normalized(root.getPropertyValue('--field-background')),
        text: normalized(root.getPropertyValue('--text')),
        border: normalized(root.getPropertyValue('--border')),
        accent: normalized(root.getPropertyValue('--accent')),
        onAccent: normalized(root.getPropertyValue('--on-accent')),
        subtle: normalized(root.getPropertyValue('--subtle')),
      },
      input: {
        background: inputStyle.backgroundColor,
        text: inputStyle.color,
        border: inputStyle.borderTopColor,
      },
      button: {background: buttonStyle.backgroundColor, text: buttonStyle.color},
      status: statusStyle.color,
    };
  });

  expect(themedStyles.input).toEqual({
    background: themedStyles.root.field,
    text: themedStyles.root.text,
    border: themedStyles.root.border,
  });
  expect(themedStyles.button).toEqual({
    background: themedStyles.root.accent,
    text: themedStyles.root.onAccent,
  });
  expect(themedStyles.status).toBe(themedStyles.root.subtle);
});
