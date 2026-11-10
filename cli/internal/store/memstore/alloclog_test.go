package memstore_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

func TestMemoryAllocLogPassesTheContract(t *testing.T) {
	storetest.RunAllocLog(t, func(*testing.T) storetest.AllocLogWorld {
		return storetest.AllocLogWorld{Log: &memstore.AllocLog{}}
	})
}
