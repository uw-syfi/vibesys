package memstore_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

func TestContract(t *testing.T) {
	storetest.Run(t, func(t *testing.T, rules store.Rules) store.Store {
		return memstore.New(memstore.WithRules(rules))
	})
}
