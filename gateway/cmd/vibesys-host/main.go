// vibesys-host serves the host API, or connects stdio to a detached serving process.
package main

import (
	"os"

	"github.com/uw-syfi/vibesys/gateway/internal/hostmain"
)

func main() { os.Exit(hostmain.Main(os.Args[1:])) }
