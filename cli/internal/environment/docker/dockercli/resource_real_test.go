//go:build docker && unix

package dockercli_test

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
)

// A real container gets what a resource attaches, as the kubernetes resource attaches a cluster: a
// file of the machine read-only at its path, the variables, and the network mode, which is the
// machine's own for "host".
func TestARealContainerGetsAResourcesFileVariablesAndNetwork(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	kubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(kubeconfig, []byte("apiVersion: v1\nkind: Config\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cluster := &fakeresource.Fake{
		Of: core.ResourceKubernetes, Facts: map[string]string{"kubernetes_server": "https://127.0.0.1:6443", "kubernetes_version": "v1.31.0"},
		Attached: resource.Attachment{
			Mounts:  []resource.Mount{{Host: kubeconfig, Container: "/vibesys/kubeconfig", ReadOnly: true}},
			Env:     map[string]string{"KUBECONFIG": "/vibesys/kubeconfig", "VIBESYS_K8S_REGISTRY": "localhost:5001"},
			Network: "host",
		},
	}
	p, err := docker.New(docker.Config{
		Engine: newEngine(), Source: source{docker.Spec{Dockerfile: []byte(dockerfile)}}, Backend: cpuBackend(),
		Timers: clock.System{}, IDs: dockercli.RandomIDs{}, Scope: scope, Placement: "local",
		User: docker.User{UID: os.Getuid(), GID: os.Getgid()}, Resources: []resource.Resource{cluster},
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := p.Environment(target)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	fp, err := env.Fingerprint(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = exec.Command("docker", "rmi", fp.ImageDigest).Run() })
	if fp.Facts["placement"] != "local" || fp.Facts["kubernetes_server"] != "https://127.0.0.1:6443" {
		t.Errorf("fingerprint facts %v lack the placement and the cluster", fp.Facts)
	}
	hostname, err := os.Hostname()
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	script := `cat "$KUBECONFIG"; echo "registry=$VIBESYS_K8S_REGISTRY"; hostname; if echo x >> "$KUBECONFIG" 2>/dev/null; then echo writable; else echo read-only; fi`
	a := env.Run(ctx, core.Execution{Checkout: core.Checkout{Commit: target.Commit, Dir: t.TempDir()}, Argv: []string{"/bin/sh", "-c", script}, Timeout: time.Minute}, &out)
	if e, ok := a.(core.Ended); !ok || e.Exec.Kind != core.ExecExited || e.Exec.ExitCode != 0 {
		t.Fatalf("Run = %+v; output:\n%s", a, out.String())
	}
	for _, want := range []string{"kind: Config", "registry=localhost:5001", hostname, "read-only"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("the container printed\n%s\nwhich lacks %q", out.String(), want)
		}
	}
}
