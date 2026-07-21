package sampling

import "testing"

func TestCaseCountIsDeterministicAndBounded(t *testing.T) {
	first, err := CaseCount(71, 32, 35)
	if err != nil {
		t.Fatal(err)
	}
	second, err := CaseCount(71, 32, 35)
	if err != nil {
		t.Fatal(err)
	}
	if first != second || first < 32 || first > 35 {
		t.Fatalf("first=%d second=%d", first, second)
	}
	for _, bounds := range [][2]int{{0, 1}, {3, 2}} {
		if _, err := CaseCount(1, bounds[0], bounds[1]); err == nil {
			t.Fatalf("invalid bounds %v were accepted", bounds)
		}
	}
}
