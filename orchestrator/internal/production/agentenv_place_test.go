package production

import (
	"math/rand/v2"
	"slices"
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// A run pinned to an eval place gives every agent that place as the placement its jobs default to,
// exactly as the run recorded it. An unpinned run says nothing of placement, neither set nor unset, so
// the placement the operator's own environment names stays; and the place changes no other name.
func TestAnAgentsEnvironmentNamesTheRunsEvalPlaceAndAnUnpinnedRunLeavesPlacementAlone(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 9))
	name := envcodec.NameOf(&cliv1.OperatorContext{}, "placement")
	for i := range 200 {
		start := sessionStart("agent-1", core.RoleLead, "/repo")
		start.JobTool = core.JobToolEndpoint{Executable: maybePath(r), RuntimeDirectory: maybePath(r)}
		without, err := agentEnvironment(start)
		if err != nil {
			t.Fatal(err)
		}
		start.EvalPlace = someWord(r, 20)
		got, err := agentEnvironment(start)
		if err != nil {
			t.Fatal(err)
		}
		if _, set := without.GetSet()[name]; set || slices.Contains(without.GetUnset(), name) {
			t.Fatalf("round %d: an unpinned run names the placement", i)
		}
		if start.EvalPlace == "" {
			if !proto.Equal(got, without) {
				t.Fatalf("round %d: an empty place changed the environment", i)
			}
			continue
		}
		if got.GetSet()[name] != start.EvalPlace || slices.Contains(got.GetUnset(), name) {
			t.Fatalf("round %d: placement %q, want %q", i, got.GetSet()[name], start.EvalPlace)
		}
		delete(got.Set, name)
		if !proto.Equal(got, without) {
			t.Fatalf("round %d: the place changed more than the placement", i)
		}
	}
}
