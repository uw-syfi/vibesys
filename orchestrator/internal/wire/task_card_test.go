package wire_test

import (
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// Every domain the contract defines has a core value and comes back from it unchanged, so a domain
// added to the contract and not to the core fails here rather than as an unreadable card.
func TestEveryTaskDomainSurvivesTheCoreTypes(t *testing.T) {
	values := cliv1.TaskDomain_TASK_DOMAIN_UNSPECIFIED.Descriptor().Values()
	for i := range values.Len() {
		domain := cliv1.TaskDomain(values.Get(i).Number())
		t.Run(domain.String(), func(t *testing.T) {
			facts := sampleSetupFacts()
			facts.Card.Domain = domain
			got, err := wire.SetupFactsIn(facts)
			if err != nil {
				t.Fatalf("a card of domain %v does not map to the core types: %v", domain, err)
			}
			if back := wire.TaskCardMessage(&got.Card).GetDomain(); back != domain {
				t.Fatalf("domain %v came back as %v", domain, back)
			}
		})
	}
}
