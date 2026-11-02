package docker_test

import (
	"context"
	"errors"
	"math/rand/v2"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
)

const digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func validSpec() docker.ContainerSpec {
	return docker.ContainerSpec{
		Name: "vibesys-cli-0123456789abcdef", Image: digest,
		Labels: docker.Labels{docker.LabelOwner: docker.Owner, docker.LabelScope: "u1000", docker.LabelRun: "0123456789abcdef"},
		User:   docker.User{UID: 1000, GID: 1000}, Dir: "/work/checkout",
		Mounts: []docker.Mount{{Host: "/work/checkout", Container: "/work/checkout"}, {Host: "/data/in", Container: "/in", ReadOnly: true}},
		Env:    map[string]string{"HOME": "/tmp", "LANG": "C"},
	}
}

func TestAValidContainerSpecIsAccepted(t *testing.T) {
	if err := validSpec().Validate(); err != nil {
		t.Fatal(err)
	}
}

// Each field a container is made from is checked, and the error names the field.
func TestAContainerSpecThatAnEngineWouldMisreadIsRefusedByName(t *testing.T) {
	cases := map[string]struct {
		mutate func(*docker.ContainerSpec)
		names  string
	}{
		"an empty name":              {func(s *docker.ContainerSpec) { s.Name = "" }, "container name"},
		"a name with a slash":        {func(s *docker.ContainerSpec) { s.Name = "a/b" }, "container name"},
		"a name that starts a flag":  {func(s *docker.ContainerSpec) { s.Name = "-rm" }, "container name"},
		"an image by tag":            {func(s *docker.ContainerSpec) { s.Image = "busybox:1.36" }, "exact digest"},
		"a short digest":             {func(s *docker.ContainerSpec) { s.Image = "sha256:abc" }, "exact digest"},
		"a negative uid":             {func(s *docker.ContainerSpec) { s.User.UID = -1 }, "negative"},
		"a relative directory":       {func(s *docker.ContainerSpec) { s.Dir = "work" }, "working directory"},
		"a mount with a comma":       {func(s *docker.ContainerSpec) { s.Mounts[0].Host = "/a,b" }, "mount 1 host path"},
		"a mount with a colon":       {func(s *docker.ContainerSpec) { s.Mounts[1].Container = "/a:b" }, "mount 2 container path"},
		"a mount with a quote":       {func(s *docker.ContainerSpec) { s.Mounts[0].Container = `/a"b` }, "mount 1 container path"},
		"a mount with a line break":  {func(s *docker.ContainerSpec) { s.Mounts[0].Host = "/a\nb" }, "mount 1 host path"},
		"a label key in capitals":    {func(s *docker.ContainerSpec) { s.Labels["Bad"] = "x" }, "label"},
		"a label value with a break": {func(s *docker.ContainerSpec) { s.Labels["ok"] = "a\nb" }, "label"},
		"an env name with a dash":    {func(s *docker.ContainerSpec) { s.Env["A-B"] = "x" }, "environment variable"},
		"an env value with a NUL":    {func(s *docker.ContainerSpec) { s.Env["A"] = "x\x00" }, "environment variable"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			spec := validSpec()
			c.mutate(&spec)
			err := spec.Validate()
			var f *core.Failure
			if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, c.names) {
				t.Fatalf("got %v, want invalid input naming %q", err, c.names)
			}
		})
	}
}

// Whatever characters a path is made of, the spec is accepted only if no character in it could end
// a field of the mount syntax.
func TestAPathIsAcceptedOnlyIfNoCharacterOfItCanEndAMountField(t *testing.T) {
	alphabet := []rune("abc/._-:,\"\n\r\x00 é")
	rng := rand.New(rand.NewPCG(3, 4))
	accepted := 0
	for range 5000 {
		var b strings.Builder
		b.WriteByte('/')
		for range rng.IntN(8) {
			b.WriteRune(alphabet[rng.IntN(len(alphabet))])
		}
		spec := validSpec()
		spec.Dir = b.String()
		spec.Mounts = []docker.Mount{{Host: b.String(), Container: b.String()}}
		err := spec.Validate()
		if bad := strings.ContainsAny(b.String(), ":,\"\x00\n\r"); (err != nil) != bad {
			t.Fatalf("path %q: error %v, but it holds a field-ending character: %v", b.String(), err, bad)
		}
		if err == nil {
			accepted++
		}
	}
	if accepted == 0 {
		t.Fatal("no generated path was accepted: the property proved nothing")
	}
}

// The fake is no more forgiving than the contract: it refuses what Validate refuses, creating
// nothing.
func TestTheFakeEngineRefusesWhatValidateRefuses(t *testing.T) {
	w := newScene(t, options{})
	w.fingerprint()
	img := w.fingerprint().ImageDigest
	spec := validSpec()
	spec.Image = img
	spec.Mounts[0].Host = "/a,b"
	if _, err := w.r.client.Start(context.Background(), spec); err == nil {
		t.Fatal("the fake started a container Validate refuses")
	}
	if len(w.daemon.Containers()) != 0 {
		t.Fatal("a refused spec created a container")
	}
}

// Every call of the engine can fail, before or after it does its work, and the fingerprint fails
// with it instead of pinning a guess; once the engine works again it is the same fingerprint.
func TestTheFingerprintFailsWithTheEngineAndRecovers(t *testing.T) {
	for _, op := range dockerfake.Ops {
		for _, late := range []bool{false, true} {
			s := newScene(t, options{})
			s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
				if c.Op == op {
					return &dockerfake.Fault{Err: boom, Late: late}
				}
				return nil
			})
			_, err := s.env.Fingerprint(context.Background())
			if op == dockerfake.OpImage || op == dockerfake.OpBuild {
				if !errors.Is(err, boom) {
					t.Errorf("%s late=%v: got %v, want the engine's failure", op, late, err)
				}
			}
			s.daemon.SetFaults(nil)
			f1, err := s.env.Fingerprint(context.Background())
			if err != nil {
				t.Fatalf("%s late=%v: %v", op, late, err)
			}
			if f2, err := s.env.Fingerprint(context.Background()); err != nil || !f1.Equal(f2) {
				t.Errorf("%s late=%v: the fingerprint changed: %v, %v", op, late, f2, err)
			}
		}
	}
}
