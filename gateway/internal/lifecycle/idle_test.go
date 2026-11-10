package lifecycle

import (
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/gateway/internal/propcheck"
)

func TestIdleGenerations(t *testing.T) {
	if err := quick.Check(func(events []bool) bool {
		state := Idle{}
		stale := []uint64{}
		active := 0
		for _, open := range events {
			stale = append(stale, state.Generation)
			if open || active == 0 {
				state = state.Opened()
				active++
			} else {
				state = state.Closed()
				active--
			}
			if state.Connections != active || state.Expired(state.Generation) != (active == 0) {
				return false
			}
			for _, generation := range stale {
				if state.Expired(generation) {
					return false
				}
			}
		}
		return true
	}, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}
