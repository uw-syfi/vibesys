package wire_test

import (
	"slices"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// The resource kinds the domain knows are exactly the contract's, in its order, named as a task
// writes them: the value's name without its prefix, in lower case.
func TestTheResourceKindsAreTheContracts(t *testing.T) {
	var contract []core.ResourceKind
	for n := int32(1); ; n++ {
		name, ok := cliv1.ResourceKind_name[n]
		if !ok {
			break
		}
		contract = append(contract, core.ResourceKind(strings.ToLower(strings.TrimPrefix(name, "RESOURCE_KIND_"))))
	}
	if len(contract) != len(cliv1.ResourceKind_name)-1 || !slices.Equal(contract, core.ResourceKinds()) {
		t.Errorf("the contract's resource kinds are %v, the domain's %v", contract, core.ResourceKinds())
	}
	for _, k := range core.ResourceKinds() {
		if got, err := core.ParseResourceKind(string(k)); err != nil || got != k {
			t.Errorf("ParseResourceKind(%q) = %q, %v", k, got, err)
		}
	}
}
