/** Source-neutral campaign presentation and stores over app-injected data services. */

export {
  type CampaignState,
  type CampaignStore,
  type CampaignStoreOptions,
  createCampaignHistoryStore,
  createCampaignReplayStore,
  createLiveCampaignStore,
  type LoadCampaign,
} from './effects/stores.js';
export {
  ArchitectureDiagram,
  type ArchitectureDiagramProps,
} from './ui/ArchitectureDiagram.js';
export {CampaignDashboard} from './ui/CampaignDashboard.js';
