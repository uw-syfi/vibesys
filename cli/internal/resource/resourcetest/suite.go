// Package resourcetest is the contract every resource.Resource passes: its kind, its claims, its
// fingerprint and what it attaches, on the happy path and when what it reads fails. Each
// implementation's tests run it, the Fake's included, so a test that uses the Fake in place of a
// real resource tests what the real one does.
package resourcetest

import (
	"context"
	"errors"
	"maps"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// Subject is one resource under the contract, made fresh for each case.
type Subject struct {
	Resource resource.Resource
	// Kind is the kind it must report.
	Kind core.ResourceKind
	// Break makes whatever the resource reads fail from now on, or is nil for a resource that reads
	// nothing.
	Break func()
}

// Run runs the contract against the resources make makes.
func Run(t *testing.T, make func(t *testing.T) Subject) {
	t.Run("it is of its kind, which is a known one", func(t *testing.T) {
		s := make(t)
		if got := s.Resource.Kind(); got != s.Kind || !slices.Contains(core.ResourceKinds(), got) {
			t.Fatalf("Kind = %q, want %q", got, s.Kind)
		}
	})
	t.Run("its claims are its kind's, positive, and the same on every call", func(t *testing.T) {
		s := make(t)
		first := s.Resource.Claims()
		for _, c := range first {
			if !strings.HasPrefix(c.Resource, string(s.Kind)+":") || len(c.Resource) == len(s.Kind)+1 || c.Amount < 1 {
				t.Errorf("claim %+v: want an id %q:<what> and a positive amount", c, s.Kind)
			}
		}
		if again := s.Resource.Claims(); !reflect.DeepEqual(first, again) {
			t.Errorf("Claims changed between calls: %+v then %+v", first, again)
		}
	})
	t.Run("its fingerprint names facts and is the same on every call", func(t *testing.T) {
		s := make(t)
		first, err := s.Resource.Fingerprint(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		if len(first) == 0 {
			t.Error("the fingerprint is empty, so it identifies nothing")
		}
		for name, value := range first {
			if !resource.FactName(name) || value == "" {
				t.Errorf("fact %q=%q: want a fact name and a value", name, value)
			}
		}
		again, err := s.Resource.Fingerprint(t.Context())
		if err != nil || !maps.Equal(first, again) {
			t.Errorf("Fingerprint changed between calls: %v then %v, %v", first, again, err)
		}
	})
	t.Run("what it attaches is valid, and the same on every call", func(t *testing.T) {
		s := make(t)
		first, err := s.Resource.Attach(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		if err := first.Validate(); err != nil {
			t.Fatal(err)
		}
		if _, err := resource.Combine([]resource.Attachment{first}); err != nil {
			t.Fatalf("Combine of one attachment: %v", err)
		}
		again, err := s.Resource.Attach(t.Context())
		if err != nil || !reflect.DeepEqual(first, again) {
			t.Errorf("Attach changed between calls: %+v then %+v, %v", first, again, err)
		}
	})
	t.Run("a call whose context is done does nothing and says so", func(t *testing.T) {
		s := make(t)
		ctx, cancel := context.WithCancel(t.Context())
		cancel()
		if _, err := s.Resource.Fingerprint(ctx); !errors.Is(err, context.Canceled) {
			t.Errorf("Fingerprint: %v, want the context's error", err)
		}
		if _, err := s.Resource.Attach(ctx); !errors.Is(err, context.Canceled) {
			t.Errorf("Attach: %v, want the context's error", err)
		}
	})
	t.Run("a resource that cannot be read is unavailable, never fingerprinted as something else", func(t *testing.T) {
		s := make(t)
		if s.Break == nil {
			return // a resource that reads nothing cannot fail to read it
		}
		s.Break()
		_, err := s.Resource.Fingerprint(t.Context())
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Errorf("Fingerprint of a broken resource: %v, want an unavailable failure", err)
		}
	})
}
