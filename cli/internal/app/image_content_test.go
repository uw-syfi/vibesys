package app_test

import (
	"context"
	"fmt"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// digest is a distinct image digest per n.
func digest(n int) string { return fmt.Sprintf("sha256:%064x", n) }

// onPlacement is a docker fingerprint of the image digest, taken on the placement place-a.
func onPlacement(imageDigest string) core.Fingerprint {
	return core.Fingerprint{
		Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: imageDigest,
		Facts: map[string]string{core.FactPlacement: "place-a"},
	}
}

// Regression: a CLI release that pins the content of the task's image in the fingerprint, where
// earlier releases pinned the image's ID, must not make every result saved before it incomparable
// with every result after it. A result that carries an image ID compares by the content of that
// image while the placement's engine still has it; one whose image is gone, or is of other
// content, or whose engine cannot be asked, stays another environment.
func TestAResultOfAnEarlierVersionComparesByTheContentOfItsImage(t *testing.T) {
	const content = 100
	images := map[string]string{
		digest(1): digest(content), // an earlier build of the content the new results pin
		digest(2): digest(content), // another build of it, with another ID
		digest(3): digest(content + 1),
	}
	for _, c := range []struct {
		name string
		// base is the image digest the base's measurement was recorded with, and candidate the
		// candidate's.
		base, candidate string
		opts            []fakeenv.Option
		comparable      bool
	}{
		{name: "an ID the engine has, against its content", base: digest(1), candidate: digest(content), comparable: true},
		{name: "two IDs of one content", base: digest(1), candidate: digest(2), comparable: true},
		{name: "two results that pin one content", base: digest(content), candidate: digest(content), comparable: true},
		{name: "an ID the engine no longer has", base: digest(4), candidate: digest(content)},
		{name: "an ID of other content", base: digest(3), candidate: digest(content)},
		{name: "an ID of other content against an ID", base: digest(3), candidate: digest(1)},
		{name: "two contents", base: digest(content + 1), candidate: digest(content)},
		{
			name: "an engine that cannot be asked", base: digest(1), candidate: digest(content),
			opts: []fakeenv.Option{fakeenv.WithImageContentError(&core.Failure{Kind: core.FailureUnavailable, Message: "engine down"})},
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			w := newWorld(t)
			b := newBranches()
			docker := fakeenv.New(core.EnvironmentDocker, w.gates, append([]fakeenv.Option{fakeenv.WithImages(images)}, c.opts...)...)
			a := app.New(app.Config{
				Clock:        w.clock,
				Stores:       w.stores,
				Environments: fakeenv.Environments{core.EnvironmentHost: w.env, core.EnvironmentDocker: docker},
				Supervisors:  w.supervisors(),
				Branches:     b,
				Logf:         func(format string, args ...any) { t.Logf("daemon: "+format, args...) },
			})
			w.apps = append(w.apps, a)
			if err := a.Start(context.Background()); err != nil {
				t.Fatalf("Start: %v", err)
			}
			st, err := w.stores.Create(context.Background(), scope)
			if err != nil {
				t.Fatal(err)
			}
			storetest.SeedMeasurement(t, st, storetest.Measurement{Commit: base, Samples: level, At: w.clock.Now(), Environment: onPlacement(c.base)})
			storetest.SeedMeasurement(t, st, storetest.Measurement{Commit: cm(1), Samples: win, At: w.clock.Now(), Environment: onPlacement(c.candidate)})

			got, err := a.Compare(ctxFor(t), call, cm(1), "")
			decision, _, promoteErr := a.Promote(ctxFor(t), call, cm(1))
			if !c.comparable {
				wantFailure(t, "compare", err, core.FailureEnvironmentMismatch)
				if promoteErr != nil || !slices.Contains(refusalKinds(t, decision), core.RefusalEnvironmentMismatch) {
					t.Errorf("Promote = %+v, %v; want refused for the environments", decision, promoteErr)
				}
				return
			}
			if err != nil || got.Verdict.Kind != core.Better {
				t.Fatalf("Compare = %+v, %v; want better", got, err)
			}
			if _, ok := decision.(core.Approved); promoteErr != nil || !ok {
				t.Errorf("Promote = %+v, %v; want approved", decision, promoteErr)
			}
		})
	}
}
