package store_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store/storetest"
)

func TestMemoryPassesTheContract(t *testing.T) {
	storetest.Contract(t, func(t *testing.T) *storetest.Harness {
		m := store.NewMemory()
		h := &storetest.Harness{Store: m}
		h.Reopen = func() store.Store { m = m.Reopen(); return m }
		return h
	})
}
