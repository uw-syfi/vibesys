// vibesys-client starts, follows, checks and stops runs on the host's orchestrator.
package main

import (
	"os"

	"github.com/uw-syfi/vibesys/gateway/internal/clientmain"
)

func main() { os.Exit(clientmain.Main(os.Args[1:])) }
