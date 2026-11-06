package docker_test

import (
	"bytes"
	"context"
	"maps"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// labIn holds a lab of x and calls during with its bench, then ends it as asked to.
func labIn(t *testing.T, env environment.Environment, x core.Execution, during func(environment.Bench)) core.Attempt {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	return env.Lab(ctx, x, nil, func(b environment.Bench) core.ExecOutcome {
		during(b)
		return core.ExecOutcome{Kind: core.ExecExited}
	})
}

// run starts argv in a room of the bench and returns its output once it exited 0.
func run(t *testing.T, room environment.Launcher, argv ...string) string {
	t.Helper()
	var out bytes.Buffer
	p, err := room.Start(argv, &out)
	if err != nil {
		t.Fatal(err)
	}
	if st := <-p.Exited(); st.Err != nil || st.Code != 0 {
		t.Fatalf("%v exited %+v", argv, st)
	}
	return out.String()
}

// A lab of a task of two images holds the containers a profile's run has, the candidate's with the
// GPUs and the task's in its network, each with the artifacts directory they share; nothing runs
// in them but what the lab starts. The containers are removed, and the artifacts brought back,
// when the lab ends.
func TestALabHoldsTheTwoContainersOfARunOfTwoImages(t *testing.T) {
	m := &remoteMachine{}
	s := newCandidateScene(t)
	s.scene = newScene(t, options{timers: s.timers, configure: func(c *docker.Config) {
		c.Candidates = s.files
		c.Probe = probeProgram
		c.Machine = m
		c.Resources = []resource.Resource{gpus, mountsOf()}
	}})
	x := core.Execution{Checkout: checkout(t), Artifacts: "/art/01-lab", Timeout: limit, Candidate: &core.CandidateRun{Image: s.candidate().ID}}
	attempt := labIn(t, s.env, x, func(b environment.Bench) {
		if got := slices.Sorted(maps.Keys(b.Rooms)); !slices.Equal(got, []core.LabRoom{core.LabCandidate, core.LabTask}) {
			t.Fatalf("rooms %v", got)
		}
		boxes := s.daemon.Containers()
		if len(boxes) != 2 {
			t.Fatalf("containers %+v, want the candidate's and the task's", boxes)
		}
		var cand, beside dockerfake.Info
		for _, box := range boxes {
			if strings.HasSuffix(box.Name, "-candidate") {
				cand = box
			} else {
				beside = box
			}
		}
		if cand.Spec.Image != x.Candidate.Image || cand.Spec.GPUs.Devices != "device=0" || beside.Spec.GPUs != (docker.GPUs{}) {
			t.Errorf("the candidate's container %+v or the task's %+v has the wrong image or GPUs", cand.Spec, beside.Spec)
		}
		if beside.Spec.Network != docker.NetworkOf(cand.Name) || !strings.HasSuffix(beside.Spec.Env[envcodec.NameOf(&cliv1.JobEnvironment{}, "service_url")], ":"+strconv.Itoa(b.Port)) {
			t.Errorf("the task's container is in %q at %q, want the candidate's network at port %d", beside.Spec.Network, beside.Spec.Env[envcodec.NameOf(&cliv1.JobEnvironment{}, "service_url")], b.Port)
		}
		for _, box := range boxes {
			if !slices.Contains(box.Spec.Mounts, docker.Mount{Host: "/far/art/01-lab", Container: docker.ArtifactsDir}) {
				t.Errorf("%s lacks the artifacts directory: %+v", box.Name, box.Spec.Mounts)
			}
			if box.Spec.Measured || box.Spec.Limit != limit {
				t.Errorf("%s is measured %v with limit %v; a lab is unmeasured, for its time", box.Name, box.Spec.Measured, box.Spec.Limit)
			}
		}
		if b.Artifacts != docker.ArtifactsDir {
			t.Errorf("the rooms see the artifacts at %q", b.Artifacts)
		}
		if got := run(t, b.Rooms[core.LabCandidate], "print", "in the candidate"); got != "in the candidate\n" {
			t.Errorf("the candidate's room said %q", got)
		}
		if got := run(t, b.Rooms[core.LabTask], "print", "in the task"); got != "in the task\n" {
			t.Errorf("the task's room said %q", got)
		}
		ctx := context.Background()
		if err := b.Replaced(ctx); err != nil {
			t.Fatal(err)
		}
		if err := b.Added(ctx, "in/x.txt"); err != nil {
			t.Fatal(err)
		}
		if err := b.Collect(ctx); err != nil {
			t.Fatal(err)
		}
	})
	if e, ok := attempt.(core.Ended); !ok || e.Exec != (core.ExecOutcome{Kind: core.ExecExited}) {
		t.Fatalf("attempt %+v", attempt)
	}
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Fatalf("containers left after the lab: %+v", left)
	}
	if len(m.replaced) != 1 || m.replaced[0].Checkout != x.Checkout.Dir || !slices.Equal(m.added, []string{"/far/art/01-lab/in/x.txt"}) {
		t.Fatalf("the machine replaced %+v and added %v", m.replaced, m.added)
	}
	if len(m.collected) != 2 {
		t.Fatalf("the artifacts were collected %d times, want once on asking and once at the end", len(m.collected))
	}
}

// A lab of a task of one image holds one container of the task's image; a lab with a command of
// its own, or no artifacts directory, is refused before anything starts.
func TestALabOfOneImageHoldsOneContainerAndRefusesWhatItCannotHold(t *testing.T) {
	s := newScene(t, options{})
	x := core.Execution{Checkout: checkout(t), Artifacts: "/art/01-lab", Timeout: limit}
	attempt := labIn(t, s.env, x, func(b environment.Bench) {
		if len(b.Rooms) != 1 || b.Rooms[core.LabCandidate] == nil || len(s.daemon.Containers()) != 1 {
			t.Fatalf("rooms %v, containers %+v", b.Rooms, s.daemon.Containers())
		}
	})
	if _, ok := attempt.(core.Ended); !ok {
		t.Fatalf("attempt %+v", attempt)
	}
	for name, bad := range map[string]core.Execution{
		"a command":       {Checkout: x.Checkout, Artifacts: x.Artifacts, Timeout: limit, Argv: []string{"x"}},
		"no artifacts":    {Checkout: x.Checkout, Timeout: limit},
		"no time":         {Checkout: x.Checkout, Artifacts: x.Artifacts},
		"measured":        {Checkout: x.Checkout, Artifacts: x.Artifacts, Timeout: limit, Measured: true},
		"a server to run": {Checkout: x.Checkout, Artifacts: x.Artifacts, Timeout: limit, Candidate: &core.CandidateRun{Image: "sha256:" + strings.Repeat("a", 64), Serve: &core.CandidateServe{Argv: []string{"s"}, Ready: "/", StartupTimeout: limit}}},
	} {
		held := false
		attempt := labIn(t, s.env, bad, func(environment.Bench) { held = true })
		if _, ok := attempt.(core.NotStarted); !ok || held {
			t.Errorf("%s: %+v, held %v; want refused before anything started", name, attempt, held)
		}
	}
}

// A placement that runs no task of two images, one with no program to probe a server with, holds
// no lab of one either, and says why before anything starts.
func TestAPlacementWithoutAProbeHoldsNoLabOfTwoImages(t *testing.T) {
	s := newCandidateScene(t)
	s.scene = newScene(t, options{timers: s.timers, configure: func(c *docker.Config) { c.Candidates = s.files }})
	x := core.Execution{Checkout: checkout(t), Artifacts: "/art/01-lab", Timeout: limit, Candidate: &core.CandidateRun{Image: s.candidate().ID}}
	held := false
	attempt := labIn(t, s.env, x, func(environment.Bench) { held = true })
	n, ok := attempt.(core.NotStarted)
	if !ok || held || !strings.Contains(n.Failure.Message, "probe") {
		t.Fatalf("attempt %+v, held %v; want refused naming the probe", attempt, held)
	}
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Fatalf("containers %+v", left)
	}
}
