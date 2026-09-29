import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {AuthStatus, ProviderAuth} from '../home-api.js';
import {keyView} from '../setup.js';
import {KeyRow} from './KeyRow.js';

const AUTH = JSON.parse(
  readFileSync(new URL('../fixtures/home-auth.json', import.meta.url), 'utf8'),
) as AuthStatus;
const row = (name: string): ProviderAuth => {
  const found = AUTH.providers.find(item => item.provider === name);
  if (found === undefined) throw new Error(name);
  return found;
};
const none = () => undefined;
const render = (provider: string, value: string, saving = false) =>
  renderToStaticMarkup(
    <KeyRow
      view={keyView(row(provider), saving ? {kind: 'saving'} : {kind: 'idle'}, '/e/.env')}
      value={value}
      saving={saving}
      onValue={none}
      onSave={none}
      onRecheck={none}
    />,
  );

test('a key field is a write-only password input with the variable and .env path on hover', () => {
  const html = render('codex', '');
  assert.match(html, /<label for="f-key">OpenAI API key<\/label>/);
  assert.match(
    html,
    /<div class="fld keyfld" title="Written to OPENAI_API_KEY in \/e\/.env"><input id="f-key" type="password" autoComplete="new-password" spellCheck="false" placeholder="Paste a key…" value=""\/>/,
  );
  assert.match(html, /Write-only\. Stored in \.env on this machine and never shown again\./);
  assert.doesNotMatch(html, />Save</);
  assert.match(render('codex', 'x'), />Save<\/button>/);
});

test('saving disables the field; a CLI-only provider shows its terminal sign-in', () => {
  const saving = render('codex', 'x', true);
  assert.match(saving, /disabled="" value="x"\/><span class="spin" aria-hidden="true"><\/span>/);
  assert.doesNotMatch(saving, />Save</);
  const cli = render('opencode', '');
  assert.match(cli, /<span class="lab">OpenCode sign-in<\/span>/);
  assert.match(
    cli,
    /<button id="f-key" type="button" class="linkish mono" title="Copy opencode auth login">opencode auth login<\/button>/,
  );
  assert.match(cli, />Check again<\/button>/);
  assert.doesNotMatch(cli, /type="password"/);
});
