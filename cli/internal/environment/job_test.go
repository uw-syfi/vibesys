package environment_test

import (
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Whatever limit a job has, the seconds stated cover it, are never a whole second too many, and are
// a value the job environment accepts.
func TestTheTimeoutIsStatedInWholeSecondsThatCoverTheLimit(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	for range 5000 {
		limit := time.Duration(r.Int64N(int64(2 * 366 * 24 * time.Hour)))
		seconds := environment.TimeoutSeconds(limit)
		job, err := environment.JobVariables(&cliv1.JobEnvironment{TimeoutSeconds: seconds})
		if err != nil {
			t.Fatalf("%v: %v", limit, err)
		}
		if limit == 0 && len(job.Set) != 0 {
			t.Fatalf("no limit is stated as %v", job.Set)
		}
		covered := time.Duration(seconds) * time.Second
		if seconds < 31536000 && (covered < limit || covered-limit >= time.Second) {
			t.Fatalf("%v is stated as %d seconds", limit, seconds)
		}
	}
	if environment.TimeoutSeconds(-time.Second) != 0 {
		t.Fatal("a negative limit states one")
	}
}

// A command is started with exactly the environment it inherits, except that the variables the job
// environment declares are the job's: set where the job states them, removed where it does not.
func TestAJobEnvironmentReplacesWhatTheCommandInherits(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	inherited := []string{"PATH=/bin", "HOME=/h", "VIBESYS_MOUNTS=left-over", "VIBESYS_CACHE_DIR=/old", "VIBESYS_ARTIFACTS_DIR=/old-artifacts", "VIBESYS_TASK=t"}
	for range 500 {
		job := &cliv1.JobEnvironment{}
		if r.IntN(2) == 0 {
			job.Artifacts = "/a"
		}
		if r.IntN(2) == 0 {
			job.TimeoutSeconds = uint32(r.IntN(1000)) + 1
		}
		variables, err := environment.JobVariables(job)
		if err != nil {
			t.Fatal(err)
		}
		got := environment.Overlay(inherited, variables)
		var read cliv1.JobEnvironment
		if err := envcodec.Decode(envcodec.FromEnviron(got), &read); err != nil {
			t.Fatal(err)
		}
		if read.GetArtifacts() != job.GetArtifacts() || read.GetTimeoutSeconds() != job.GetTimeoutSeconds() || read.GetCache() != "" || len(read.GetMounts()) != 0 {
			t.Fatalf("job %v: the command reads %v from %v", job, &read, got)
		}
		for _, kept := range []string{"PATH=/bin", "HOME=/h", "VIBESYS_TASK=t"} {
			if !slices.Contains(got, kept) {
				t.Fatalf("%s was dropped from %v", kept, got)
			}
		}
		if strings.Count(strings.Join(got, "\n"), "VIBESYS_ARTIFACTS_DIR=") > 1 {
			t.Fatalf("a variable is set twice in %v", got)
		}
	}
}
