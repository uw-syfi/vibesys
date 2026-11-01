package core_test

import (
	"encoding/json"
	"errors"
	"math"
	"os"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// vector is one case of testdata/compare_vectors.json: an input and what Python's vs_core.compare
// answered for it. A null expectation is vs_core returning no comparison.
type vector struct {
	Name          string    `json:"name"`
	Note          string    `json:"note"`
	Candidate     []float64 `json:"candidate"`
	Base          []float64 `json:"base"`
	Direction     string    `json:"direction"`
	RelativeNoise float64   `json:"relative_noise"`
	Expected      *struct {
		Verdict          string  `json:"verdict"`
		CandidateMean    float64 `json:"candidate_mean"`
		BaseMean         float64 `json:"base_mean"`
		Ratio            float64 `json:"ratio"`
		Noise            float64 `json:"noise"`
		CandidateSamples uint32  `json:"candidate_samples"`
		BaseSamples      uint32  `json:"base_samples"`
	} `json:"expected"`
}

func loadVectors(t *testing.T) []vector {
	t.Helper()
	raw, err := os.ReadFile("testdata/compare_vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Vectors []vector `json:"vectors"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	return file.Vectors
}

func directionOf(t *testing.T, name string) core.Direction {
	t.Helper()
	switch name {
	case "max":
		return core.Maximize
	case "min":
		return core.Minimize
	}
	t.Fatalf("vector direction %q is neither max nor min", name)
	return ""
}

func near(got, want, relative, absolute float64) bool {
	return math.Abs(got-want) <= math.Max(absolute, relative*math.Abs(want))
}

// TestVectors runs every case the Python implementation answered. Verdicts and counts match
// exactly; numbers match to a relative 1e-9, and the noise also to an absolute 1e-12.
func TestVectors(t *testing.T) {
	vectors := loadVectors(t)
	if len(vectors) != 56 {
		t.Fatalf("testdata holds %d vectors, want the 56 vs_core produced", len(vectors))
	}
	for _, v := range vectors {
		t.Run(v.Name, func(t *testing.T) {
			got, err := core.Compare(core.CompareInput{
				Direction:     directionOf(t, v.Direction),
				Candidate:     v.Candidate,
				Base:          v.Base,
				RelativeNoise: v.RelativeNoise,
			})
			if v.Expected == nil {
				var failure *core.Failure
				if !errors.As(err, &failure) || failure.Kind != core.FailureInvalidInput {
					t.Fatalf("want an invalid_input failure for a comparison that does not exist, got %v (verdict %+v)", err, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("Compare: %v", err)
			}
			want := v.Expected
			if string(got.Kind) != want.Verdict {
				t.Errorf("verdict %s, want %s (%s)", got.Kind, want.Verdict, v.Note)
			}
			if got.CandidateSamples != want.CandidateSamples || got.BaseSamples != want.BaseSamples {
				t.Errorf("sample counts %d and %d, want %d and %d", got.CandidateSamples, got.BaseSamples, want.CandidateSamples, want.BaseSamples)
			}
			for name, pair := range map[string][2]float64{
				"candidate mean": {got.CandidateMean, want.CandidateMean},
				"base mean":      {got.BaseMean, want.BaseMean},
				"ratio":          {got.Ratio, want.Ratio},
			} {
				if !near(pair[0], pair[1], 1e-9, 0) {
					t.Errorf("%s %v, want %v", name, pair[0], pair[1])
				}
			}
			if !near(got.Noise, want.Noise, 1e-9, 1e-12) {
				t.Errorf("noise %v, want %v", got.Noise, want.Noise)
			}
		})
	}
}
