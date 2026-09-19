import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)
//
// Doc ids are the repo docs/ paths (the docs plugin `path` points at ../docs),
// so this sidebar and the docs/ tree have the same shape: user-facing pages at
// the top level, contributor pages under docs/contributing/.
const sidebars: SidebarsConfig = {
  docsSidebar: [
    'running-vibesys',
    'cli-flags',
    {
      type: 'category',
      label: 'Contributing',
      link: {type: 'doc', id: 'contributing/development'},
      items: [
        'contributing/development',
        'contributing/coding-best-practices',
        'contributing/architecture',
        'contributing/agent-drivers',
        'contributing/domains',
        'contributing/feature-flags',
        'contributing/extending-profilers',
        'contributing/skill-metadata',
        'contributing/openevolve',
        'contributing/remote-slurm-execution',
        'contributing/issue-authoring',
        'contributing/publishing',
      ],
    },
  ],
};

export default sidebars;
