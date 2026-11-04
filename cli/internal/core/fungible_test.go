package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Placements are interchangeable: where each sample ran never decides a verdict. These properties
// draw saved results in docker environments that share or differ in image digest, all taken on one
// placement, then move every result to a placement, machine and set of resources drawn at random,
// and ask every rule that compares or reuses results again: the comparison of two commits, the
// promotion, the frontier and the cache. Each answers as it did on the one placement, failures
// included.

var (
	fungibleDigests = []string{fmt.Sprintf("sha256:%064x", 1), fmt.Sprintf("sha256:%064x", 2)}
	fungiblePlaces  = []string{"default", "local", "a100", "kind-1"}
)

// placedOn is a docker environment of the digest that ran on a placement drawn at random: its
// machine, its placement and the facts of the resources it was given.
func placedOn(r *rand.Rand, digest string) core.Fingerprint {
	f := core.Fingerprint{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: digest, Facts: map[string]string{
		core.FactPlacement: fungiblePlaces[r.IntN(len(fungiblePlaces))],
		"os":               "linux",
		"arch":             []string{"amd64", "arm64"}[r.IntN(2)],
		"cpu_model":        fmt.Sprintf("cpu-%d", r.IntN(3)),
		"logical_cpus":     fmt.Sprint(1 + r.IntN(64)),
		"image_platform":   "linux/amd64",
	}}
	if r.IntN(2) == 0 {
		f.Facts["gpu_model"] = []string{"A100", "MI210"}[r.IntN(2)]
	}
	if r.IntN(2) == 0 {
		f.Facts["kubernetes_server"] = fmt.Sprintf("v1.%d", 28+r.IntN(3))
	}
	return f
}

// onOnePlacement is a docker environment of the digest on the placement "default", with nothing
// but the machine's facts.
func onOnePlacement(digest string) core.Fingerprint {
	return core.Fingerprint{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: digest, Facts: map[string]string{
		core.FactPlacement: "default", "os": "linux", "arch": "amd64", "cpu_model": "cpu-0", "logical_cpus": "8", "image_platform": "linux/amd64",
	}}
}

// relabel moves every result to a placement drawn at random, keeping its image digest, and keys it
// as a store would have keyed it there.
func relabel(t testing.TB, r *rand.Rand, results []core.Result) []core.Result {
	t.Helper()
	out := make([]core.Result, len(results))
	for i, res := range results {
		res.Environment = placedOn(r, res.Environment.ImageDigest)
		res.Key = keyFor(t, res)
		out[i] = res
	}
	return out
}

// keyFor is the key a result of its kind is saved under in its environment.
func keyFor(t testing.TB, res core.Result) core.JobKey {
	t.Helper()
	kind, settings := core.JobKind(core.JobCheck), core.Settings(core.CheckSettings{})
	if _, ok := res.Outcome.(core.MeasureOutcome); ok {
		kind, settings = core.JobMeasure, core.MeasureSettings{}
	}
	return keyOf(t, core.KeyInputs{
		Kind: kind, Commit: res.Commit, TaskVersion: res.TaskVersion, Environment: res.Environment,
		Settings: settings, SchemaVersion: res.SchemaVersion,
	})
}

// fungibleResults draws measurements and check passes of the candidate and the base, each in an
// image of one of two digests, mostly the first, on a placement drawn at random.
func fungibleResults(t testing.TB, r *rand.Rand) []core.Result {
	t.Helper()
	var out []core.Result
	for i := range 2 + r.IntN(5) {
		commit := []core.Commit{candidateCommit, baseCommit}[i%2]
		digest := fungibleDigests[0]
		if r.IntN(8) == 0 {
			digest = fungibleDigests[1]
		}
		var res core.Result
		if r.IntN(5) == 0 {
			res = checkPass(fmt.Sprintf("c%d", i), commit, i+1)
		} else {
			res = measurement(fmt.Sprintf("m%d", i), commit, samplesIn(r, 3+r.IntN(5), 100+float64(r.IntN(3))*5, 0.03), i+1)
		}
		res.Environment = onOnePlacement(digest)
		res.Key = keyFor(t, res)
		out = append(out, res)
	}
	return out
}

// verdictOf is what a comparison decided, without the environment it names, which is one of the
// results' and says where that one ran.
func verdictOf(c core.Comparison, err error) string {
	c.Environment = core.Fingerprint{}
	return fmt.Sprintf("%+v %v", c, failureKind(err))
}

func decisionOf(d core.PromotionDecision, err error) string {
	switch d := d.(type) {
	case core.Approved:
		p := d.Promotion()
		p.Environment = core.Fingerprint{}
		return fmt.Sprintf("approved %+v", p)
	case core.PromoteRefusal:
		var kinds []core.PromoteRefusalKind
		for _, reason := range d.Reasons {
			kinds = append(kinds, reason.Kind)
		}
		return fmt.Sprintf("refused %v", kinds)
	}
	return fmt.Sprintf("%T %v", d, failureKind(err))
}

func TestComparisonsAndPromotionsDoNotDependOnWhereEachSampleRan(t *testing.T) {
	r := newRand(201)
	for range properties {
		results := fungibleResults(t, r)
		floor := []float64{0, 0.02}[r.IntN(2)]
		compare := func(rs []core.Result) string {
			return verdictOf(core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: rs, RelativeNoise: floor}))
		}
		promote := func(rs []core.Result) string {
			return decisionOf(core.DecidePromotion(core.PromotionRequest{Candidate: candidateCommit, Head: baseCommit, Results: rs, RelativeNoise: floor, At: at(100)}))
		}
		want, wantPromotion := compare(results), promote(results)
		for range 4 {
			moved := relabel(t, r, results)
			if got := compare(moved); got != want {
				t.Fatalf("moving the samples to other placements changed the comparison\nfrom %s\nto   %s\nfor %+v", want, got, moved)
			}
			if got := promote(moved); got != wantPromotion {
				t.Fatalf("moving the samples to other placements changed the promotion\nfrom %s\nto   %s", wantPromotion, got)
			}
		}
	}
}

func TestTheCacheReusesWhatRanOnAnyPlacement(t *testing.T) {
	r := newRand(202)
	for range properties {
		saved := fungibleResults(t, r)
		asked := onOnePlacement(fungibleDigests[r.IntN(2)])
		samples := uint32(1 + r.IntN(12))
		answers := func(rs []core.Result, env core.Fingerprint) string {
			var b []string
			for _, settings := range []core.Settings{core.CheckSettings{}, core.MeasureSettings{Samples: samples}} {
				key := keyOf(t, core.KeyInputs{Kind: settings.Kind(), Commit: candidateCommit, TaskVersion: "t1", Environment: env, Settings: settings, SchemaVersion: core.SchemaVersion})
				got, err := core.PlanCache(core.CacheQuery{Key: key, Settings: settings, DefaultSamples: 5, Saved: rs})
				b = append(b, fmt.Sprintf("%+v %v", got, failureKind(err)))
			}
			return fmt.Sprint(b)
		}
		want := answers(saved, asked)
		for range 4 {
			if got := answers(relabel(t, r, saved), placedOn(r, asked.ImageDigest)); got != want {
				t.Fatalf("moving the saved results and the job to other placements changed the cache's answer\nfrom %s\nto   %s", want, got)
			}
		}
	}
}

func TestTheFrontierDoesNotDependOnWhereEachSampleRan(t *testing.T) {
	r := newRand(203)
	for range properties / 4 {
		w := newFrontierWorld(t)
		var commits []core.Commit
		for i := range 2 + r.IntN(5) {
			commits = append(commits, w.measure(i+1, gridPoint(r), inEnvironment(onOnePlacement(fungibleDigests[0]))))
		}
		order := r.Perm(len(commits))
		frontierOf := func(results []core.Result) []core.Commit {
			w.results, w.frontier, w.minute = results, core.Frontier{}, 0
			for _, i := range order {
				w.add(commits[i])
			}
			return w.commits()
		}
		original := slices.Clone(w.results)
		want := frontierOf(original)
		for range 3 {
			moved := slices.Clone(original)
			for i := range moved {
				moved[i].Environment = placedOn(r, moved[i].Environment.ImageDigest)
			}
			if got := frontierOf(moved); !slices.Equal(got, want) {
				t.Fatalf("moving the samples to other placements changed the frontier from %v to %v", want, got)
			}
		}
	}
}
