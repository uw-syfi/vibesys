import {describe, expect, test} from 'bun:test';
import {CODE_IDS, roleName} from '@vibesys/contracts';
import {replyTitle} from './reply-fields.js';

/** A reply class name that leads with the role's last words, as the backend names them. */
function kindOf(role: string, suffix: string): string {
  const last = role.split('-').at(-1) ?? role;
  return `${last.charAt(0).toUpperCase()}${last.slice(1)}${suffix}`;
}

describe('the title of a reply', () => {
  test('a reply that names its role by the class name reads as the role’s catalog name', () => {
    for (const role of CODE_IDS.roles) {
      expect(replyTitle(kindOf(role, 'Response'), role)).toBe(
        `${roleName(role)} response`.replace(/^./, first => first.toUpperCase()),
      );
    }
  });

  test('a reply of another kind, or of an unknown role, keeps the words of its class name', () => {
    expect(replyTitle('PreRoundDecision', 'multi-judge')).toBe('Pre round decision');
    expect(replyTitle('JudgeResponse', null)).toBe('Judge response');
    expect(replyTitle('judge_result', 'plugin-unknown')).toBe('Judge result');
  });

  test('a role the kind names after other words is named in place', () => {
    expect(replyTitle('IssueImplementerResult', 'plain-implementer')).toBe(
      `Issue ${roleName('plain-implementer').toLowerCase()} result`,
    );
  });
});
