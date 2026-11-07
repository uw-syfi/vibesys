package wire

import (
	"maps"
	"slices"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// HarnessModels is the models one harness runs, its default first.
type HarnessModels struct {
	Harness core.Harness
	Models  []core.ModelName
}

// ModelCatalogIn reads what each harness runs from a catalog. An entry for a harness this build does
// not know is left out: it cannot be launched here, so it is no choice.
func ModelCatalogIn(c *pb.ModelCatalog) []HarnessModels {
	var out []HarnessModels
	for _, entry := range c.GetHarnesses() {
		var errs errorList
		harness := harnesses.in(entry.GetHarness(), &errs)
		if errs.err() != nil {
			continue
		}
		out = append(out, HarnessModels{Harness: harness, Models: modelsIn(entry.GetModels())})
	}
	return out
}

// ModelCatalogMessage is the catalog a client is shown: the policy's allowed models and what each
// harness runs, harnesses in the order of their names so that the same policy is always the same bytes.
func ModelCatalogMessage(p core.ModelPolicy) *pb.ModelCatalog {
	catalog := &pb.ModelCatalog{AllowedModels: modelsOut(p.Allowed), UnreadablePolicy: p.Closed}
	for _, harness := range slices.Sorted(maps.Keys(p.Known)) {
		catalog.Harnesses = append(catalog.Harnesses, &pb.HarnessModels{Harness: harnesses.out(harness), Models: modelsOut(p.Known[harness])})
	}
	return catalog
}
