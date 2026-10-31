import {subscriptionPortContract} from '@vibesys/subscription/contract';
import {hubPortRig} from '../testing/hub-port-rig.js';

subscriptionPortContract('Subscriptions.port', hubPortRig);
