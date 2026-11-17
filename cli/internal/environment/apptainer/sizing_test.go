package apptainer_test

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
)

// What an allocation asks of a node follows the GPU resource the cluster has: with a GRES type,
// the GPUs the job asked for (all of the node's for a measure, which also holds the node); without
// one, nothing to ask in, so every allocation holds a whole node and the job's devices are the
// environment's to hand it. A placement with no GPUs asks for none.
func TestAnAllocationIsSizedByTheGPUsTheJobAsked(t *testing.T) {
	base := []string{"--partition=p", "--account=a"}
	cases := []struct {
		name     string
		sizing   apptainer.Sizing
		measured bool
		asked    int
		want     []string
	}{
		{"a check of 2 on a cluster with GRES", apptainer.Sizing{GPUs: 8, GresType: "mi300x"}, false, 2, []string{"--gres=gpu:mi300x:2", "--cpus-per-task=8"}},
		{"a check that asked for no count", apptainer.Sizing{GPUs: 8, GresType: "mi300x", CPUs: 4, Memory: "32G"}, false, 0, []string{"--gres=gpu:mi300x:8", "--cpus-per-task=4", "--mem=32G"}},
		{"a check that asked for more than the node has", apptainer.Sizing{GPUs: 4, GresType: "mi300x"}, false, 8, []string{"--gres=gpu:mi300x:4", "--cpus-per-task=8"}},
		{"a measure takes every GPU and the node", apptainer.Sizing{GPUs: 8, GresType: "mi300x"}, true, 2, []string{"--gres=gpu:mi300x:8", "--exclusive"}},
		{"a check on a cluster with no GRES holds the node", apptainer.Sizing{GPUs: 8}, false, 2, []string{"--exclusive"}},
		{"a measure on a cluster with no GRES", apptainer.Sizing{GPUs: 8}, true, 0, []string{"--exclusive"}},
		{"a placement with no GPUs", apptainer.Sizing{}, false, 2, []string{"--cpus-per-task=8"}},
	}
	for _, c := range cases {
		c.sizing.Partition, c.sizing.Arguments = "p", []string{"--account=a"}
		want := slices.Concat(base[:1], c.want, base[1:])
		if got := c.sizing.OptionsFor(c.measured, c.asked); !slices.Equal(got, want) {
			t.Errorf("%s: %q, want %q", c.name, got, want)
		}
	}
	// A standing or reused allocation is sized for the whole node.
	if got, want := (apptainer.Sizing{GPUs: 8, GresType: "mi300x"}).Options(true), (apptainer.Sizing{GPUs: 8, GresType: "mi300x"}).OptionsFor(true, 3); !slices.Equal(got, want) {
		t.Errorf("Options(true) = %q, want the measured sizing %q", got, want)
	}
}
