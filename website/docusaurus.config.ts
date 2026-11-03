import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const config: Config = {
  title: 'VibeSys',
  tagline: 'Generating Bespoke Systems with AI Agents',
  favicon: 'img/favicon.ico',

  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Production URL. GitHub Pages for the uw-syfi/vibesys repo, served under
  // the custom subdomain docs.vibing.systems (see static/CNAME).
  url: 'https://docs.vibing.systems',
  baseUrl: '/',

  organizationName: 'uw-syfi',
  projectName: 'vibesys',

  // Fail the build instead of warning. `warn` let broken links ship: the CI
  // website build stayed green while the deployed site served dead links
  // (docs/ links that escape the docs plugin's content root resolve to site
  // URLs that were never built).
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  stylesheets: [
    {
      href: 'https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap',
      type: 'text/css',
    },
  ],

  // docs/ moved contributor pages under docs/contributing/ so the repo tree
  // mirrors the site. These are the pre-move URLs; keep them resolving rather
  // than turning previously published links into 404s.
  plugins: [
    [
      '@docusaurus/plugin-client-redirects',
      {
        redirects: [
          {from: '/docs/contributing/devcontainer-features', to: '/docs/contributing/development'},
          ...['contributing/desktop-connection', 'contributing/wire-protocol', 'contributing/run-transport', 'contributing/kernel3-contract-decisions', 'design/strategies-on-core', 'design/launch', 'design/iteration-navigation', 'design/core-agent-evaluation-bridge'].map(id => ({from: `/docs/${id}`, to: '/docs/contributing/architecture'})),
          ...[
          'development',
          'coding-best-practices',
          'extending-profilers',
          'skill-metadata',
          'openevolve',
          'issue-authoring',
          'publishing',
        ].map(id => ({from: `/docs/${id}`, to: `/docs/contributing/${id}`})),
        ],
      },
    ],
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          // Serve the repository's docs/ directly so there is a single copy
          // of these Markdown files (no duplication under website/).
          path: '../docs',
          sidebarPath: './sidebars.ts',
          editUrl: ({docPath}) =>
            `https://github.com/uw-syfi/vibesys/tree/main/docs/${docPath}`,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'VibeSys',
      logo: {
        alt: 'VibeSys',
        src: 'img/logo.svg',
        srcDark: 'img/logo-dark.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://arxiv.org/abs/2605.06068',
          label: 'Paper',
          position: 'right',
        },
        {
          href: 'https://github.com/uw-syfi/vibesys',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Running VibeSys', to: '/docs/running-vibesys'},
            {label: 'CLI flags', to: '/docs/cli-flags'},
            {label: 'Contributing', to: '/docs/contributing/development'},
          ],
        },
        {
          title: 'Project',
          items: [
            {
              label: 'VibeServe blog post',
              href: 'https://syfi.cs.washington.edu/blog/2026-05-12-introducing-vibeserve/',
            },
            {label: 'Paper (arXiv)', href: 'https://arxiv.org/abs/2605.06068'},
            {label: 'SyFI Lab', href: 'https://syfi.cs.washington.edu/'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'GitHub', href: 'https://github.com/uw-syfi/vibesys'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} University of Washington SyFI Lab. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.vsLight,
      darkTheme: prismThemes.vsDark,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
