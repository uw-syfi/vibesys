package apptainer_test

import (
	"context"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/backend/amd"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/rocm"
)

// nodeGPUs is the node's output of amd.Script on a node with two MI210s.
const nodeGPUs = `==driver==
6.16.13
==rocminfo==
  Name:                    AMD EPYC 7V13 64-Core Processor
  Device Type:             CPU
  Name:                    gfx90a
  Marketing Name:          AMD Instinct MI210
  Device Type:             GPU
  Name:                    gfx90a
  Marketing Name:          AMD Instinct MI210
  Device Type:             GPU
==amd-smi==
`

// checked is the rocm resource with a check the fake world runs in place of the shell's.
type checked struct{ *rocm.ROCm }

func (c checked) Attach(ctx context.Context) (resource.Attachment, error) {
	a, err := c.ROCm.Attach(ctx)
	a.Check = []string{"mark", "checked"}
	return a, err
}

// A task of the rocm backend gets the node's AMD GPUs: its box runs with them after their check,
// and its fingerprint names the GPUs and the driver the node said, read inside the allocation.
func TestARocmTaskGetsTheNodesAMDGPUs(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.Say("gpu", nodeGPUs)
	r := newRig(t, w, cluster, setup{scripts: map[string]string{"gpu": amd.Script}, configure: func(c *docker.Config) {
		g, err := rocm.New(rocm.Config{Machine: "part-a", Facts: apptainer.Facts{
			Engine: c.Engine.(*apptainer.Engine), Script: "gpu", Parse: amd.Parse,
			Unread: map[string]string{amd.FactModel: apptainer.Unread, amd.FactCount: apptainer.Unread, amd.FactDriver: apptainer.Unread},
		}})
		if err != nil {
			t.Fatal(err)
		}
		c.Resources = []resource.Resource{checked{g}}
		c.Source = dockerfake.Fixed(docker.Spec{Dockerfile: []byte(dockerfile), Backend: core.BackendROCm})
	}})
	before, err := r.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if before.Facts[amd.FactModel] != apptainer.Unread {
		t.Errorf("facts before any node %v, want them unread", before.Facts)
	}
	a, _ := run(t, r.env, "exit", "0")
	exited(t, a, 0)
	if !w.Marked("checked") {
		t.Error("the GPUs' check never ran")
	}
	boxes := cluster.Started()
	if len(boxes) == 0 {
		t.Fatal("no box started")
	}
	for _, b := range boxes {
		if !b.ROCm {
			t.Errorf("box %s ran without the GPUs", b.Name)
		}
	}
	f, err := r.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if f.Facts[amd.FactModel] != "AMD Instinct MI210" || f.Facts[amd.FactCount] != "2" || f.Facts[amd.FactDriver] != "6.16.13" {
		t.Errorf("facts %v", f.Facts)
	}
}
