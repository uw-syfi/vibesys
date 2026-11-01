// Command vibesys-cli is the tool layer the VibeSys optimization skills call for anything that must
// be deterministic: measuring, comparing, promoting and job lifecycle.
//
// This file owns argument parsing and output only. Behavior lives under internal/.
package main

import (
	"fmt"
	"io"
	"os"
)

// version is the release version. Release builds set it with
// -ldflags "-X main.version=<version>"; any other build reports "dev".
var version = "dev"

// Exit codes. They match the contract in the project README: 2 is invalid input, so a caller can
// tell a mistyped command from a failed job.
const (
	exitOK           = 0
	exitFailure      = 1
	exitInvalidInput = 2
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run executes one invocation and returns its exit code. It takes its streams as parameters so a
// test drives it in process.
func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 1 && args[0] == "--version" {
		_, _ = fmt.Fprintf(stdout, "vibesys-cli %s\n", version)
		return exitOK
	}
	if len(args) > 0 && args[0] == "upgrade" {
		return runUpgrade(args[1:], stdout, stderr, productionUpgrade)
	}
	_, _ = fmt.Fprintln(stderr, "usage: vibesys-cli --version | upgrade")
	return exitInvalidInput
}
