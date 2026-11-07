/** The settings screen's tabs, in the order the left list shows them. A tab exists only with content. */
export const SETTINGS_TABS = ['appearance', 'resources'] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number];
