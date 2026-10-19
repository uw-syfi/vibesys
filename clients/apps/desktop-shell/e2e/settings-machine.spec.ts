import {expect, test} from '@playwright/test';
import {done, main, nav, openSection, openSettingsAt} from './settings-machine.js';

/**
 * Settings scoped to a machine: the Machine selector, and under it each machine's Clusters and
 * clouds and Agent logins, read from that machine's helper. The fake helper answers for three
 * machines: This Mac (set up), host-b (nothing set up, Claude Code not signed in) and host-c (a
 * VibeSys too old to say).
 */

const card = (page: import('@playwright/test').Page, name: string | RegExp) =>
  main(page).locator('.vs-card', {has: page.getByRole('heading', {name})});

test('the machine selector is at the top, with the machine sections above This app', async ({
  page,
}) => {
  await openSettingsAt(page, 1440);
  const items = await nav(page).evaluate(element =>
    [
      ...element.querySelectorAll(
        'label, button:not(.vs-section-toggle), h2, span.vs-sidebar-title',
      ),
    ].map(node => node.textContent?.trim() ?? ''),
  );
  expect(items.indexOf('Machine')).toBe(0);
  expect(items.indexOf('Clusters and clouds')).toBeLessThan(items.indexOf('This app'));
  expect(items.indexOf('Agent logins')).toBeLessThan(items.indexOf('This app'));
  for (const name of ['Appearance', 'Machines', 'Notifications']) {
    expect(items.findIndex(item => item.startsWith(name))).toBeGreaterThan(
      items.indexOf('This app'),
    );
  }
  await expect(nav(page).getByRole('combobox', {name: 'Machine'}).locator('option')).toHaveText([
    'This Mac',
    'host-b',
    'host-c',
  ]);
});

test('Clusters and clouds lists a set-up machine’s clusters, Modal, SkyPilot and where it is stored', async ({
  page,
}) => {
  await openSettingsAt(page, 1440);
  await openSection(page, 'Clusters and clouds');
  await expect(main(page).getByText('File', {exact: true})).toHaveAttribute(
    'title',
    'Stored on This Mac',
  );
  await expect(main(page).getByText(/compute\.toml$/)).toBeVisible();
  await expect(main(page).getByRole('heading', {name: 'lab-h100'})).toBeVisible();
  await expect(main(page).getByRole('heading', {name: 'campus'})).toBeVisible();
  await expect(main(page).getByRole('heading', {name: 'cluster-m'})).toBeVisible();
  await expect(main(page).getByText('From the operator file')).toBeVisible();
  await expect(main(page).getByText('SSH · lab-login')).toBeVisible();
  await expect(main(page).getByText('Login node', {exact: true})).toBeVisible();
  await expect(card(page, 'Modal').getByText('Token found')).toBeVisible();
  await expect(card(page, 'SkyPilot').getByText('Preview')).toBeVisible();
  // A cluster of the operator file is listed and never removed here.
  const operator = main(page).locator('.vs-card', {
    has: page.getByRole('heading', {name: 'cluster-m'}),
  });
  await expect(operator.getByRole('button', {name: 'Remove'})).toHaveAttribute(
    'aria-disabled',
    'true',
  );
});

test('another machine shows its own settings: empty, no Modal token, or too old to say', async ({
  page,
}) => {
  await openSettingsAt(page, 1440);
  await openSection(page, 'Clusters and clouds', 'host-b');
  await expect(main(page).getByText('No Slurm cluster yet')).toBeVisible();
  await expect(card(page, 'Modal').getByText('No token')).toBeVisible();
  await expect(main(page).getByRole('heading', {name: 'lab-h100'})).toHaveCount(0);
  await nav(page).getByRole('combobox', {name: 'Machine'}).selectOption({label: 'host-c'});
  await expect(main(page).getByText('Needs an update', {exact: true})).toBeVisible();
  await expect(main(page).getByTitle(/^The VibeSys on host-c is too old for this/)).toBeVisible();
  await nav(page).getByRole('button', {name: 'Agent logins'}).click();
  await expect(main(page).getByText('Needs an update', {exact: true})).toBeVisible();
  await expect(main(page).getByTitle(/^The VibeSys on host-c is too old for this/)).toBeVisible();
});

test('a cluster is saved only after Test connection passes, and is then listed and removable', async ({
  page,
}) => {
  await openSettingsAt(page, 1440);
  await openSection(page, 'Clusters and clouds', 'host-b');
  await main(page).getByRole('button', {name: 'Add a cluster'}).click();
  const form = card(page, 'Add a Slurm cluster');
  const save = form.getByRole('button', {name: 'Save cluster'});
  await expect(save).toHaveAttribute('aria-disabled', 'true');
  await expect(form.getByText('Test the connection first.')).toBeAttached();

  await form.getByRole('textbox', {name: 'Name'}).fill('lab-h100');
  await form.getByRole('textbox', {name: 'Workspace on the cluster'}).fill('/scratch/vibesys');
  await form.getByRole('textbox', {name: 'SSH host alias'}).fill('lab-login');
  await form.getByRole('button', {name: 'Test connection'}).click();
  await expect(form.getByText('The connection works.')).toBeVisible();
  // The test fills the hardware the cluster's scheduler lists.
  await expect(form.getByRole('combobox', {name: 'GPU vendor'})).toHaveValue('cuda');
  await expect(form.getByRole('textbox', {name: 'GPU models'})).toHaveValue('nvidia_h100');
  await expect(form.getByRole('textbox', {name: 'GPUs per node'})).toHaveValue('8');
  await expect(save).not.toHaveAttribute('aria-disabled', 'true');

  // Changing what was tested takes the test back.
  await form.getByRole('textbox', {name: 'Workspace on the cluster'}).fill('/scratch/other');
  await expect(save).toHaveAttribute('aria-disabled', 'true');
  await expect(form.getByText('Test the connection again.', {exact: true})).toBeVisible();
  await form.getByRole('button', {name: 'Test connection'}).click();
  await expect(form.getByText('The connection works.')).toBeVisible();

  await save.click();
  await expect(main(page).getByRole('heading', {name: 'lab-h100'})).toBeVisible();
  await expect(form).toHaveCount(0);
  expect((await done(page)).added).toEqual(['ssh:host-b lab-h100']);

  await main(page).getByRole('button', {name: 'Remove'}).click();
  await expect(main(page).getByText('No Slurm cluster yet')).toBeVisible();
  expect((await done(page)).removed).toEqual(['ssh:host-b lab-h100']);
});

for (const [alias, says] of [
  ['unreachable-host', 'Could not reach the cluster'],
  ['no-scheduler', 'The cluster’s scheduler did not answer'],
  ['no-workspace', 'The workspace is not usable'],
] as const) {
  test(`a connection test that fails at ${alias} says where and cannot be saved`, async ({
    page,
  }) => {
    await openSettingsAt(page, 1440);
    await openSection(page, 'Clusters and clouds', 'host-b');
    await main(page).getByRole('button', {name: 'Add a cluster'}).click();
    const form = card(page, 'Add a Slurm cluster');
    await form.getByRole('textbox', {name: 'Name'}).fill('lab');
    await form.getByRole('textbox', {name: 'Workspace on the cluster'}).fill('/w');
    await form.getByRole('textbox', {name: 'SSH host alias'}).fill(alias);
    await form.getByRole('button', {name: 'Test connection'}).click();
    await expect(form.getByText(says)).toBeVisible();
    await expect(form.getByRole('button', {name: 'Save cluster'})).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    expect((await done(page)).added).toEqual([]);
  });
}

test('a missing field is named under its own control, and nothing is sent', async ({page}) => {
  await openSettingsAt(page, 1440);
  await openSection(page, 'Clusters and clouds', 'host-b');
  await main(page).getByRole('button', {name: 'Add a cluster'}).click();
  const form = card(page, 'Add a Slurm cluster');
  await form.getByRole('button', {name: 'Test connection'}).click();
  await expect(form.getByRole('textbox', {name: 'Name'})).toHaveAttribute('aria-invalid', 'true');
  await expect(form.getByText('A cluster needs a name.')).toBeVisible();
  expect((await done(page)).tested).toEqual([]);
  await form.getByRole('button', {name: 'Cancel'}).click();
  await expect(form).toHaveCount(0);
});

test('Agent logins says each harness is signed in, not signed in, or not installed, and how', async ({
  page,
}) => {
  await openSettingsAt(page, 1440);
  await openSection(page, 'Agent logins');
  const row = (name: string) =>
    main(page).locator('.vs-card', {has: page.getByRole('heading', {name})});
  await expect(row('Claude Code').getByText('Signed in')).toBeVisible();
  await expect(row('Claude Code').getByText('Account', {exact: true})).toBeVisible();
  await expect(row('Codex').getByText('API key', {exact: true}).first()).toBeVisible();
  await expect(row('Codex').getByText('OPENAI_API_KEY', {exact: true})).toBeVisible();
  await expect(
    row('Gemini CLI').getByText('.gemini/oauth_creds.json', {exact: true}),
  ).toBeVisible();
  await expect(row('GitHub Copilot').getByText('Not signed in')).toBeVisible();
  await expect(row('OpenCode').getByText('Cannot tell')).toBeVisible();
  await expect(row('Aider').getByText('Not installed', {exact: true})).toBeVisible();
  await expect(row('Aider').getByRole('button', {name: 'Sign in'})).toHaveAttribute(
    'aria-disabled',
    'true',
  );
  await expect(row('Gemini CLI').getByRole('button', {name: 'Sign in'})).toHaveAttribute(
    'aria-disabled',
    'true',
  );
  // No secret is ever on the page.
  expect(await main(page).innerText()).not.toMatch(/sk-[A-Za-z0-9]/);
});

test('Sign in starts the harness’s own login on the chosen machine, and Check again reads it anew', async ({
  page,
}) => {
  await openSettingsAt(page, 1440);
  await openSection(page, 'Agent logins', 'host-b');
  const claude = main(page).locator('.vs-card', {
    has: page.getByRole('heading', {name: 'Claude Code'}),
  });
  await expect(claude.getByText('Not signed in')).toBeVisible();
  await claude.getByRole('button', {name: 'Sign in'}).click();
  await expect(claude.getByText('The login started on host-b.')).toBeVisible();
  await expect(
    claude.getByText('Open https://example.com/device and enter the code AB12'),
  ).toBeVisible();
  expect((await done(page)).logins).toEqual(['ssh:host-b claude']);
  await claude.getByRole('button', {name: 'Check again'}).click();
  await expect(claude.getByText('The login started on host-b.')).toHaveCount(0);
});
