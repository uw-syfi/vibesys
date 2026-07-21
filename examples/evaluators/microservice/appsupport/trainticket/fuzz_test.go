package trainticket

import (
	"math/rand"
	"strings"
	"testing"
	"time"
)

func TestGeneratedValuesCoverModeNeutralGrammar(t *testing.T) {
	random := rand.New(rand.NewSource(7))
	token := Token(random, "namespace", 3)
	if name := StationName(random, false, token); !strings.Contains(name, token) {
		t.Fatal("station grammar omitted token")
	}
	basic, first := PriceRates(random)
	if basic < 0.1 || basic > 0.9 || first < 0.9 || first > 1.9 {
		t.Fatalf("price rates out of grammar: %f %f", basic, first)
	}
	start, end := TripTimes(random)
	if end <= start {
		t.Fatalf("trip times are not ordered: %d %d", start, end)
	}
}

func TestAdminTokenUsesFreshModeNeutralIdentity(t *testing.T) {
	first, err := AdminToken(time.Unix(1_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	second, err := AdminToken(time.Unix(1_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	if first == second || strings.Contains(first, "accuracy") || strings.Contains(first, "benchmark") {
		t.Fatalf("identity is not mode neutral")
	}
}
