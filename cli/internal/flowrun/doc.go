// Package flowrun is the shell of the check and measure flows: it carries out the requests
// flow's machine makes, on a host.
//
// Everything the machine asks for happens here and nowhere else. A clean checkout and a scratch
// directory come from the interfaces named in Config, a command runs through
// environment.Environment, the machine's facts and load come from backend.Backend, a result file
// is read through hostfs.FS, and the time of a command's start and end is read from the injected
// clock. The package starts no goroutine and reads no clock of its own, so a run on fakes is
// deterministic and a run on the real parts differs only in what the parts do.
//
// A Shell holds the checkout and the scratch directory until it is closed.
package flowrun
