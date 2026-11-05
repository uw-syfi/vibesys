package kubernetes_test

import (
	"context"
	"errors"
	"maps"
	"slices"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/kubernetes"
	"github.com/uw-syfi/vibesys/cli/internal/resource/resourcetest"
)

// cluster is a Fake kubernetes.Cluster: the identity of each kubeconfig it knows, until it is
// down.
type cluster struct {
	mu   sync.Mutex
	of   map[string]kubernetes.Identity
	down bool
}

func (c *cluster) Identify(_ context.Context, kubeconfig string) (kubernetes.Identity, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	id, ok := c.of[kubeconfig]
	if c.down || !ok {
		return kubernetes.Identity{}, &core.Failure{Kind: core.FailureUnavailable, Message: "the connection to the server was refused"}
	}
	return id, nil
}

func (c *cluster) stop() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.down = true
}

const kubeconfig = "/home/u/.kube/kind-a"

func kind() *cluster {
	return &cluster{of: map[string]kubernetes.Identity{kubeconfig: {Server: "https://127.0.0.1:41000", Version: "v1.31.0"}}}
}

func config(c kubernetes.Cluster) kubernetes.Config {
	return kubernetes.Config{
		Kubeconfig: kubeconfig, Registry: "localhost:5001", Builder: "tcp://127.0.0.1:1234", Network: "host",
		Machine: "local", Cluster: c,
	}
}

func TestAClusterPassesTheContract(t *testing.T) {
	resourcetest.Run(t, func(t *testing.T) resourcetest.Subject {
		c := kind()
		k, err := kubernetes.New(config(c))
		if err != nil {
			t.Fatal(err)
		}
		return resourcetest.Subject{Resource: k, Kind: core.ResourceKubernetes, Break: c.stop}
	})
}

// A container gets the kubeconfig read-only at the path KUBECONFIG names, the network mode, and
// the registry and the builder; an address left out sets no variable.
func TestAContainerGetsTheKubeconfigTheNetworkAndTheAddresses(t *testing.T) {
	k, err := kubernetes.New(config(kind()))
	if err != nil {
		t.Fatal(err)
	}
	got, err := k.Attach(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	want := resource.Attachment{
		Mounts:  []resource.Mount{{Host: kubeconfig, Container: kubernetes.ConfigPath, ReadOnly: true}},
		Env:     map[string]string{"KUBECONFIG": kubernetes.ConfigPath, "VIBESYS_K8S_REGISTRY": "localhost:5001", "VIBESYS_K8S_BUILDER": "tcp://127.0.0.1:1234"},
		Network: "host",
	}
	if !slices.Equal(got.Mounts, want.Mounts) || !maps.Equal(got.Env, want.Env) || got.Network != want.Network || got.GPUs != want.GPUs {
		t.Errorf("Attach = %+v\nwant %+v", got, want)
	}
	cfg := config(kind())
	cfg.Registry, cfg.Builder, cfg.Network = "", "", ""
	k, err = kubernetes.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	got, err = k.Attach(t.Context())
	if err != nil || !maps.Equal(got.Env, map[string]string{"KUBECONFIG": kubernetes.ConfigPath}) || got.Network != "" {
		t.Errorf("with no addresses: %+v, %v", got, err)
	}
}

// Two clusters that differ in server or version fingerprint differently.
func TestTheFingerprintIsTheServerAndItsVersion(t *testing.T) {
	seen := map[string]kubernetes.Identity{}
	for _, id := range []kubernetes.Identity{
		{Server: "https://127.0.0.1:41000", Version: "v1.31.0"}, {Server: "https://127.0.0.1:41001", Version: "v1.31.0"},
		{Server: "https://127.0.0.1:41000", Version: "v1.30.2"},
	} {
		k, err := kubernetes.New(config(&cluster{of: map[string]kubernetes.Identity{kubeconfig: id}}))
		if err != nil {
			t.Fatal(err)
		}
		fp, err := k.Fingerprint(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		key := fp[kubernetes.FactServer] + " " + fp[kubernetes.FactVersion]
		if _, dup := seen[key]; dup {
			t.Errorf("%+v fingerprints like %+v", id, seen[key])
		}
		seen[key] = id
	}
	k, err := kubernetes.New(config(&cluster{of: map[string]kubernetes.Identity{kubeconfig: {Server: "https://x"}}}))
	if err != nil {
		t.Fatal(err)
	}
	var f *core.Failure
	if _, err := k.Fingerprint(t.Context()); !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Errorf("a cluster with no version: %v, want unavailable", err)
	}
}

func TestASettingThatCannotBeIsRefused(t *testing.T) {
	for name, change := range map[string]func(*kubernetes.Config){
		"a relative kubeconfig":   func(c *kubernetes.Config) { c.Kubeconfig = "kube/config" },
		"a kubeconfig with colon": func(c *kubernetes.Config) { c.Kubeconfig = "/a:b" },
		"a registry with a space": func(c *kubernetes.Config) { c.Registry = "localhost 5001" },
		"a network with a space":  func(c *kubernetes.Config) { c.Network = "host --privileged" },
		"no cluster reader":       func(c *kubernetes.Config) { c.Cluster = nil },
		"no machine":              func(c *kubernetes.Config) { c.Machine = "" },
	} {
		cfg := config(kind())
		change(&cfg)
		_, err := kubernetes.New(cfg)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v, want invalid input", name, err)
		}
	}
}

func TestTheServerVersionIsReadFromKubectl(t *testing.T) {
	const out = `{"clientVersion":{"gitVersion":"v1.31.1"},"kustomizeVersion":"v5.4.2","serverVersion":{"major":"1","minor":"31","gitVersion":"v1.31.0"}}`
	if v, err := kubernetes.ParseVersion(out); err != nil || v != "v1.31.0" {
		t.Errorf("ParseVersion = %q, %v", v, err)
	}
	for _, bad := range []string{`{"clientVersion":{"gitVersion":"v1.31.1"}}`, `not json`, `{"serverVersion":{}}`} {
		var f *core.Failure
		if _, err := kubernetes.ParseVersion(bad); !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Errorf("%s: %v, want unavailable", bad, err)
		}
	}
}

// Every job that runs with the cluster holds it alone: the scheduler cannot budget what a job's pods
// take of it, so two checks on one cluster could starve each other.
func TestAJobHoldsTheClusterAlone(t *testing.T) {
	k, err := kubernetes.New(config(kind()))
	if err != nil {
		t.Fatal(err)
	}
	want := []core.Claim{{Resource: "kubernetes:local" + kubeconfig, Amount: 1, Exclusive: true}}
	if got := k.Claims(); !slices.Equal(got, want) {
		t.Fatalf("Claims = %+v, want %+v", got, want)
	}
}
