package release_test

import (
	"fmt"
	"math/rand/v2"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

func TestRelateOrdersReleasesAsSemanticVersions(t *testing.T) {
	older, newer, same, unknown := pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER, pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER,
		pb.ReleaseRelation_RELEASE_RELATION_SAME, pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN
	// Each row lists versions from oldest to newest.
	for _, chain := range [][]string{
		{"0.2.0-rc.1", "0.2.0-rc.2", "0.2.0-rc.10", "0.2.0", "0.2.1", "0.10.0", "1.0.0"},
		{"1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"},
	} {
		for i, a := range chain {
			for j, b := range chain {
				want := same
				if i < j {
					want = older
				} else if i > j {
					want = newer
				}
				if got := release.Relate(a, b); got != want {
					t.Errorf("Relate(host %s, client %s) = %v, want %v", a, b, got, want)
				}
			}
		}
	}
	for _, pair := range [][2]string{{"dev", "1.0.0"}, {"1.0.0", "dev"}, {"", "1.0.0"}, {"1.0.0", ""}, {"dev", "dev"}, {"1.0", "1.0.0"}, {"1.0.0-", "1.0.0"}, {"v1.0.0", "1.0.0"}, {"1.x.0", "1.0.0"}} {
		if got := release.Relate(pair[0], pair[1]); got != unknown {
			t.Errorf("Relate(%q, %q) = %v, want unknown", pair[0], pair[1], got)
		}
	}
}

// Swapping host and client swaps older and newer, and nothing else.
func TestRelateIsAntisymmetric(t *testing.T) {
	rng := rand.New(rand.NewPCG(3, 5))
	pres := []string{"", "-rc.1", "-rc.2", "-alpha", "-1"}
	pick := func() string {
		return fmt.Sprintf("%d.%d.%d%s", rng.IntN(3), rng.IntN(3), rng.IntN(3), pres[rng.IntN(len(pres))])
	}
	flip := map[pb.ReleaseRelation]pb.ReleaseRelation{
		pb.ReleaseRelation_RELEASE_RELATION_SAME:       pb.ReleaseRelation_RELEASE_RELATION_SAME,
		pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER: pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER,
		pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER: pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER,
	}
	for range 2000 {
		a, b := pick(), pick()
		ab, ba := release.Relate(a, b), release.Relate(b, a)
		if want, ok := flip[ab]; !ok || ba != want {
			t.Fatalf("Relate(%s,%s)=%v but Relate(%s,%s)=%v", a, b, ab, b, a, ba)
		}
		if (a == b) != (ab == pb.ReleaseRelation_RELEASE_RELATION_SAME) {
			t.Fatalf("Relate(%s,%s)=%v", a, b, ab)
		}
	}
}
