package docker_test

import (
	"context"
	"fmt"
	"io"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// racingBuilds is one Docker's builds as BuildKit makes them: a build takes time, and one that
// ends after another build took its tag fails to name its image ("already exists"). The first
// build holds until gate is closed; every build says on entered that it started.
type racingBuilds struct {
	gate    chan struct{}
	entered chan string
	n       atomic.Int32
}

// racingEngine is one connection to the fake daemon whose builds race as racingBuilds says.
type racingEngine struct {
	*dockerfake.Client
	race *racingBuilds
}

func (e racingEngine) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	_, before, err := e.Image(ctx, spec.Tag)
	if err != nil {
		return err
	}
	e.race.entered <- spec.Tag
	if e.race.n.Add(1) == 1 {
		select {
		case <-e.race.gate:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if _, after, err := e.Image(ctx, spec.Tag); err != nil || (after && !before) {
		if err == nil {
			err = &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("ERROR: failed to solve: image %q: already exists", spec.Tag)}
		}
		return err
	}
	return e.Client.Build(ctx, spec, log)
}

// sayer passes on a log and closes said when the log first holds the text.
type sayer struct {
	text string
	said chan struct{}

	mu   sync.Mutex
	log  strings.Builder
	once sync.Once
}

func (s *sayer) Write(p []byte) (int, error) {
	s.mu.Lock()
	s.log.Write(p)
	found := strings.Contains(s.log.String(), s.text)
	s.mu.Unlock()
	if found {
		s.once.Do(func() { close(s.said) })
	}
	return len(p), nil
}

// Regression: each placement's provider had a build lock of its own, so jobs of two placements
// whose images one Docker builds (the apptainer placements of one machine) built one candidate
// image at once, and the build that ended second failed with "image ... already exists". Now the
// second waits for the first and uses its image, and the image is built once.
func TestJobsOfTwoPlacementsOnOneEngineBuildOneCandidateImageOnce(t *testing.T) {
	daemon := dockerfake.NewDaemon(fakeworld.New().MemoryGates)
	race := &racingBuilds{gate: make(chan struct{}), entered: make(chan string, 2)}
	files := &candidateFiles{files: map[string]string{"serve/Dockerfile": "FROM busybox:1.36.1\nCOPY . /srv\n"}}
	shared := docker.NewBuilds()
	envs := map[string]environment.Environment{}
	for _, placement := range []string{"gpu-a", "gpu-b"} {
		r := newRig(t, daemon, options{configure: func(c *docker.Config) {
			c.Engine = racingEngine{Client: c.Engine.(*dockerfake.Client), race: race}
			c.Placement, c.Scope = placement, "test-"+placement
			c.Candidates, c.Probe, c.Builds = files, probeProgram, shared
		}})
		envs[placement] = r.env(t)
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()

	type result struct {
		img core.CandidateImage
		err error
	}
	results := make(chan result, 2)
	candidate := func(env environment.Environment, log io.Writer) {
		img, err := env.Candidate(ctx, build, log)
		results <- result{img, err}
	}
	var wg sync.WaitGroup
	defer wg.Wait()
	wg.Go(func() { candidate(envs["gpu-a"], io.Discard) })
	select {
	case <-race.entered:
	case <-ctx.Done():
		t.Fatal("the first job never started its build")
	}
	second := &sayer{text: "waiting for it to end", said: make(chan struct{})}
	wg.Go(func() { candidate(envs["gpu-b"], second) })
	// The second job either builds too, or waits for the first: either way the first may end.
	select {
	case <-race.entered:
	case <-second.said:
	case <-ctx.Done():
		t.Fatal("the second job neither built nor waited")
	}
	close(race.gate)

	var got []result
	for range 2 {
		select {
		case r := <-results:
			got = append(got, r)
		case <-ctx.Done():
			t.Fatal("a job never ended")
		}
	}
	for _, r := range got {
		if r.err != nil {
			t.Fatalf("a job failed: %v", r.err)
		}
	}
	if got[0].img != got[1].img {
		t.Fatalf("the jobs got two images: %+v and %+v", got[0].img, got[1].img)
	}
	if n := daemon.Builds(); n != 1 {
		t.Fatalf("the image was built %d times, want once", n)
	}
}
