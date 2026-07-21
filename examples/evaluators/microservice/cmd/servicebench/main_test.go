package main

import "testing"

func TestParseSeed(t *testing.T) {
	if seed, err := parseSeed("42", "--fixture-seed"); err != nil || seed != 42 {
		t.Fatalf("parseSeed() = %d, %v", seed, err)
	}
	if _, err := parseSeed("not-a-seed", "--fixture-seed"); err == nil {
		t.Fatal("parseSeed accepted a non-integer")
	}
	first, err := parseSeed("random", "--fixture-seed")
	if err != nil || first < 0 {
		t.Fatalf("random seed = %d, %v", first, err)
	}
}
