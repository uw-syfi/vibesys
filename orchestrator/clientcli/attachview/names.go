package attachview

import (
	"fmt"
	"slices"
	"strconv"
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// AgentName is the name a person reads for an agent: its role and its number among the agents of
// that role in the order they were started, such as planner-2 or worker-1. The lead, of which a run
// has one, is just lead. An agent the view does not have is named by its id, which is all that is known.
func (v *View) AgentName(id string) string {
	if len(v.names) != len(v.Agents) {
		v.names = nameAgents(v.Agents)
	}
	if name, ok := v.names[id]; ok {
		return name
	}
	return Sanitize(id)
}

func nameAgents(agents map[string]*pb.Agent) map[string]string {
	ordered := make([]*pb.Agent, 0, len(agents))
	for _, a := range agents {
		ordered = append(ordered, a)
	}
	slices.SortFunc(ordered, func(a, b *pb.Agent) int {
		if c := a.GetStartedAt().AsTime().Compare(b.GetStartedAt().AsTime()); c != 0 {
			return c
		}
		return strings.Compare(a.GetId(), b.GetId())
	})
	names := make(map[string]string, len(agents))
	seen := map[pb.AgentRole]int{}
	for _, a := range ordered {
		role := StatusWord(a.GetRole().String(), "AGENT_ROLE_")
		seen[a.GetRole()]++
		if a.GetRole() == pb.AgentRole_AGENT_ROLE_LEAD && seen[a.GetRole()] == 1 {
			names[a.GetId()] = role
			continue
		}
		names[a.GetId()] = role + "-" + strconv.Itoa(seen[a.GetRole()])
	}
	return names
}

// AgentsInOrder lists the run's agents with the lead first and the rest in the order they started.
func (v *View) AgentsInOrder() []*pb.Agent {
	out := make([]*pb.Agent, 0, len(v.Agents))
	for _, a := range v.Agents {
		out = append(out, a)
	}
	lead := v.Run.GetLeadAgentId()
	slices.SortFunc(out, func(a, b *pb.Agent) int {
		if (a.GetId() == lead) != (b.GetId() == lead) {
			if a.GetId() == lead {
				return -1
			}
			return 1
		}
		if c := a.GetStartedAt().AsTime().Compare(b.GetStartedAt().AsTime()); c != 0 {
			return c
		}
		return strings.Compare(a.GetId(), b.GetId())
	})
	return out
}

// ResolveAgent finds the agent a person named: by the name the view shows (planner-2), by its full
// id, or by a prefix of its id that no other agent shares. The error lists the names the run has.
func (v *View) ResolveAgent(text string) (string, error) {
	text = strings.TrimSpace(text)
	if text != "" {
		for id := range v.Agents {
			if strings.EqualFold(v.AgentName(id), text) || id == text {
				return id, nil
			}
		}
		var prefixed []string
		for id := range v.Agents {
			if strings.HasPrefix(id, text) {
				prefixed = append(prefixed, id)
			}
		}
		if len(prefixed) == 1 {
			return prefixed[0], nil
		}
	}
	var have []string
	for _, a := range v.AgentsInOrder() {
		have = append(have, v.AgentName(a.GetId()))
	}
	return "", fmt.Errorf("run %s has no agent %q (or more than one); its agents are %s", v.RunID, text, strings.Join(have, ", "))
}
