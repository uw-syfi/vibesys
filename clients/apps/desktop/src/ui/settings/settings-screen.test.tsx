import {describe, expect, it} from '@effect/vitest';
import {initialAppearance} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {SettingsScreen} from '../index.js';

installDom();

const appearance = initialAppearance('light');

describe('the public Settings screen', () => {
  it('organizes the real desktop settings and diagnostics into operator sections', () => {
    const screen = mount(
      <SettingsScreen
        appearance={appearance}
        choose={() => undefined}
        viewer={{id: 'install-7', label: 'Ana'}}
        machines={{count: 3, manage: () => undefined, add: () => undefined}}
        diagnostics={{protocolVersion: '12'}}
      />,
    );

    const sections = [...screen.container.querySelectorAll('.vs-card-title')].map(
      heading => heading.textContent,
    );
    expect(sections).toEqual([
      'Appearance',
      'Accounts & providers',
      'Runs',
      'Machines & servers',
      'About & diagnostics',
    ]);
    expect(screen.text()).toContain('Ana');
    expect(screen.text()).toContain('install-7');
    expect(screen.text()).toContain('Host protocol12');
    expect(screen.text()).toContain('Machines in this window3');
    screen.unmount();
  });

  it('routes machine management through the callbacks owned by the shell', () => {
    const moved: string[] = [];
    const screen = mount(
      <SettingsScreen
        appearance={appearance}
        choose={() => undefined}
        viewer={{id: 'install-7', label: 'Ana'}}
        machines={{
          count: 1,
          manage: () => moved.push('machines'),
          add: () => moved.push('add-machine'),
        }}
        diagnostics={{protocolVersion: '12'}}
      />,
    );

    screen.press(screen.control('Review provider machines'));
    screen.press(screen.control('Manage machines'));
    screen.press(screen.control('Add machine or server'));
    expect(moved).toEqual(['machines', 'machines', 'add-machine']);
    screen.unmount();
  });

  it('states unsupported run capabilities instead of rendering inert controls', () => {
    const screen = mount(
      <SettingsScreen appearance={appearance} choose={() => undefined} viewer={null} />,
    );

    expect(screen.text()).toContain('does not deliver desktop run notifications');
    expect(screen.text()).toContain('Runs cannot be archived or restored here yet');
    expect(screen.container.querySelectorAll('[role="switch"]').length).toBe(0);
    expect(screen.container.querySelectorAll('input[type="checkbox"]').length).toBe(0);
    expect(screen.text()).toContain('Operator labelUnavailable');
    expect(screen.text()).not.toContain('Sign in');
    screen.unmount();
  });
});
