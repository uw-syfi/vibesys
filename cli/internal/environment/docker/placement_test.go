package docker_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
)

// clusterOf is a resource that attaches a cluster the way the kubernetes resource does: a file,
// read-only, the variables, and a network mode.
func clusterOf(server string) *fakeresource.Fake {
	return &fakeresource.Fake{
		Of: core.ResourceKubernetes, Facts: map[string]string{"kubernetes_server": server, "kubernetes_version": "v1.31.0"},
		Attached: resource.Attachment{
			Mounts:  []resource.Mount{{Host: "/home/u/.kube/kind", Container: "/vibesys/kubeconfig", ReadOnly: true}},
			Env:     map[string]string{"KUBECONFIG": "/vibesys/kubeconfig", "VIBESYS_K8S_REGISTRY": "localhost:5001"},
			Network: "host",
		},
		Claimed: []core.Claim{{Resource: "kubernetes:local/home/u/.kube/kind", Amount: 1}},
	}
}

func keptContainer(s *scene) {
	s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
		if c.Op == dockerfake.OpRemove {
			return &dockerfake.Fault{Err: errors.New("kept for the test")}
		}
		return nil
	})
}

// A container gets what every resource of its placement attaches, beside what the run gives it,
// and the fingerprint pins the placement and what each resource reports.
func TestAContainerGetsWhatItsPlacementsResourcesAttach(t *testing.T) {
	s := newScene(t, options{
		spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Needs: []core.ResourceKind{core.ResourceKubernetes}},
		configure: func(c *docker.Config) {
			c.Placement = "local"
			c.Resources = []resource.Resource{clusterOf("https://127.0.0.1:41000"), mountsOf()}
		},
	})
	f, err := s.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range map[string]string{"placement": "local", "kubernetes_server": "https://127.0.0.1:41000", "kubernetes_version": "v1.31.0", "mounts": "hf-cache,models"} {
		if f.Facts[k] != v {
			t.Errorf("fact %s = %q, want %q", k, f.Facts[k], v)
		}
	}
	keptContainer(s)
	_ = runIn(t, s, core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour})
	spec := onlyContainer(t, s.daemon)
	if spec.Network != "host" {
		t.Errorf("network %q, want host", spec.Network)
	}
	for _, m := range []docker.Mount{
		{Host: "/home/u/.kube/kind", Container: "/vibesys/kubeconfig", ReadOnly: true},
		{Host: "/data/hf", Container: "/vibesys/mounts/hf-cache", ReadOnly: true},
	} {
		if !slices.Contains(spec.Mounts, m) {
			t.Errorf("mounts %+v lack %+v", spec.Mounts, m)
		}
	}
	for k, v := range map[string]string{"KUBECONFIG": "/vibesys/kubeconfig", "VIBESYS_K8S_REGISTRY": "localhost:5001", "VIBESYS_MOUNTS": "hf-cache,models", "HOME": "/tmp"} {
		if spec.Env[k] != v {
			t.Errorf("env %s = %q, want %q", k, spec.Env[k], v)
		}
	}
}

// A task that needs a kind of resource its placement does not give is refused before anything is
// built or started, naming the placement and the resource, whatever else the placement gives.
func TestATaskThatNeedsWhatThePlacementLacksIsRefusedNamingBoth(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 6))
	makers := map[core.ResourceKind]func() resource.Resource{
		core.ResourceGPU:        func() resource.Resource { return gpuOf(someGPU) },
		core.ResourceKubernetes: func() resource.Resource { return clusterOf("https://k") },
		core.ResourceMounts:     func() resource.Resource { return mountsOf() },
		core.ResourceROCm:       func() resource.Resource { return amdOf() },
	}
	for range 60 {
		var given, needs []core.ResourceKind
		for _, k := range core.ResourceKinds() {
			if r.IntN(2) == 0 {
				given = append(given, k)
			}
			if r.IntN(2) == 0 {
				needs = append(needs, k)
			}
		}
		var missing core.ResourceKind
		for _, n := range needs {
			if !slices.Contains(given, n) {
				missing = n
				break
			}
		}
		s := newScene(t, options{
			spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Needs: needs},
			configure: func(c *docker.Config) {
				c.Placement = "a100"
				for _, k := range given {
					c.Resources = append(c.Resources, makers[k]())
				}
			},
		})
		_, err := s.env.Fingerprint(context.Background())
		a := runIn(t, s, core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour})
		if missing == "" {
			if err != nil {
				t.Fatalf("given %v, needs %v: %v", given, needs, err)
			}
			continue
		}
		want := fmt.Sprintf("placement %q has no resource %q", "a100", missing)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, want) {
			t.Fatalf("given %v, needs %v: Fingerprint %v, want %q", given, needs, err, want)
		}
		if got := envtest.NotStarted(t, a); !strings.Contains(got.Message, want) {
			t.Fatalf("given %v, needs %v: Run %v, want %q", given, needs, got, want)
		}
		if s.daemon.Builds() != 0 || len(s.daemon.Containers()) != 0 {
			t.Fatal("something was built or started")
		}
	}
}

// Two placements, or two resources that report anything different, never fingerprint alike, so
// their results are never compared or reused; the same placement and resources always do.
func TestTheFingerprintTellsPlacementsAndResourcesApart(t *testing.T) {
	type setup struct {
		placement string
		server    string
		gpu       bool
	}
	r := rand.New(rand.NewPCG(7, 8))
	seen := map[string]setup{}
	for range 80 {
		u := setup{placement: []string{"default", "local", "a100"}[r.IntN(3)], server: []string{"https://a", "https://b"}[r.IntN(2)], gpu: r.IntN(2) == 0}
		fp := func() core.Fingerprint {
			s := newScene(t, options{configure: func(c *docker.Config) {
				c.Placement = u.placement
				c.Resources = []resource.Resource{clusterOf(u.server)}
				if u.gpu {
					c.Resources = append(c.Resources, gpuOf(someGPU))
				}
			}})
			f, err := s.env.Fingerprint(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			return f
		}
		f := fp()
		if again := fp(); !again.Equal(f) {
			t.Fatalf("%+v fingerprints as %v, then %v", u, f, again)
		}
		key := f.String()
		if other, ok := seen[key]; ok && other != u {
			t.Fatalf("%+v and %+v fingerprint alike: %v", u, other, f)
		}
		seen[key] = u
	}
}

// A resource that cannot be read fails the fingerprint as unavailable, and a resource that would
// take a place or a variable of the run's own is refused before the container starts.
func TestAResourceThatFailsOrCollidesStopsTheRun(t *testing.T) {
	broken := clusterOf("https://k")
	broken.Break()
	s := newScene(t, options{configure: func(c *docker.Config) { c.Resources = []resource.Resource{broken} }})
	var f *core.Failure
	if _, err := s.env.Fingerprint(context.Background()); !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Errorf("a broken resource: %v, want unavailable", err)
	}
	for name, a := range map[string]resource.Attachment{
		"a variable of the run's":   {Env: map[string]string{"HOME": "/root"}},
		"the cache directory":       {Mounts: []resource.Mount{{Host: "/x", Container: docker.CacheDir}}},
		"a mount of another's path": {Mounts: []resource.Mount{{Host: "/x", Container: "/vibesys/kubeconfig"}}},
	} {
		s := newScene(t, options{configure: func(c *docker.Config) {
			c.Machine = &remoteMachine{}
			c.Resources = []resource.Resource{clusterOf("https://k"), &fakeresource.Fake{Of: core.ResourceMounts, Facts: map[string]string{"mounts": "x"}, Attached: a}}
		}})
		got := envtest.NotStarted(t, runIn(t, s, core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}))
		if got.Kind != core.FailureInvalidInput || len(s.daemon.Containers()) != 0 {
			t.Errorf("%s: %+v, want invalid input and no container", name, got)
		}
	}
}
