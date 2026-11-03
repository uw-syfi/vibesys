package hostmain_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/gateway/internal/hostmain"
)

func TestHelpReturnsSuccess(t *testing.T) {
	for _, command := range []string{"serve", "connect"} {
		t.Run(command, func(t *testing.T) {
			if got := hostmain.Main([]string{command, "--help"}); got != 0 {
				t.Fatalf("Main = %d", got)
			}
		})
	}
}
