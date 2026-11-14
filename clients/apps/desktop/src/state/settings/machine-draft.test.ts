import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {emptyMachineDraft, machineDraftResult} from './machine-draft.js';
import {parseSshCommand} from './ssh-target.js';

describe('the structured machine draft', () => {
  it('names the required field of every connection method', () => {
    expect(machineDraftResult(emptyMachineDraft)).toMatchObject({ready: false, field: 'alias'});
    expect(machineDraftResult({...emptyMachineDraft, kind: 'direct'})).toMatchObject({
      ready: false,
      field: 'host',
    });
    expect(machineDraftResult({...emptyMachineDraft, kind: 'command'})).toMatchObject({
      ready: false,
      field: 'command',
    });
  });

  it('builds a canonical direct SSH command with every supported optional field', () => {
    const result = machineDraftResult({
      ...emptyMachineDraft,
      kind: 'direct',
      host: 'host-a',
      user: 'operator',
      port: '2202',
      jump: 'jump-a',
      identityFile: '~/.ssh/ops key',
      repositoriesDirectory: '  /srv/vibesys  ',
    });
    expect(result).toMatchObject({
      ready: true,
      target: {
        destination: 'operator@host-a',
        port: 2202,
        jump: 'jump-a',
        identityFile: '~/.ssh/ops key',
      },
      repositoriesDirectory: '/srv/vibesys',
    });
    if (result.ready) expect(parseSshCommand(result.input)).toEqual(result.target);
  });

  it('accepts an SSH config alias and a pasted command through the same closed parser', () => {
    expect(machineDraftResult({...emptyMachineDraft, alias: 'gpu-a'})).toMatchObject({
      ready: true,
      target: {destination: 'gpu-a'},
    });
    expect(
      machineDraftResult({
        ...emptyMachineDraft,
        kind: 'command',
        command: 'ssh -p 2222 user@host-a',
      }),
    ).toMatchObject({ready: true, target: {destination: 'user@host-a', port: 2222}});
  });

  it('refuses an invalid port and every shell-bearing command', () => {
    fc.assert(
      fc.property(
        fc.integer().filter(port => port < 1 || port > 65_535),
        port => {
          expect(
            machineDraftResult({
              ...emptyMachineDraft,
              kind: 'direct',
              host: 'host-a',
              port: String(port),
            }),
          ).toMatchObject({ready: false, field: 'port'});
        },
      ),
    );
    expect(
      machineDraftResult({...emptyMachineDraft, kind: 'command', command: 'ssh host-a; reboot'}),
    ).toMatchObject({ready: false, field: 'command'});
  });

  it('names the exact direct or workspace field that needs correction', () => {
    const direct = {...emptyMachineDraft, kind: 'direct' as const, host: 'host-a'};
    expect(machineDraftResult({...direct, user: 'bad user'})).toMatchObject({
      ready: false,
      field: 'user',
    });
    expect(machineDraftResult({...direct, jump: '-jump-a'})).toMatchObject({
      ready: false,
      field: 'jump',
    });
    expect(machineDraftResult({...direct, identityFile: '-key'})).toMatchObject({
      ready: false,
      field: 'identityFile',
    });
    expect(machineDraftResult({...direct, repositoriesDirectory: 'relative/repos'})).toMatchObject({
      ready: false,
      field: 'repositoriesDirectory',
    });
  });
});
