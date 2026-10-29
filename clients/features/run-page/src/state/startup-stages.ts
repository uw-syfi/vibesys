/** Startup words are shared by recorded lifecycle stages and legacy stream owners. */
export const STARTUP_LABELS = {
  probing_workspace: 'Probing the workspace',
  building_image: 'Building the agent image',
  building_tools: 'Building the agent tools',
  starting_container: 'Starting the agent container',
} as const;
