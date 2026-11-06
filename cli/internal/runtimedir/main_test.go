package runtimedir_test

import (
	"os"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/shorttmp"
)

// TestMain keeps TMPDIR short enough for the sockets these tests bind: see shorttmp.
func TestMain(m *testing.M) { os.Exit(shorttmp.Run(m)) }
