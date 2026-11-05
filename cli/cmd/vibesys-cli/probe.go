package main

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// probeTimeout bounds one probe: a server that does not answer in it is not ready yet, and the
// environment probes again after its pause, within the server's startup time.
const probeTimeout = 10 * time.Second

// probeCommand is `probe-ready <url>`, which no person calls: the docker environment mounts this
// static binary into the container of a task's image and runs it there to ask whether the
// candidate's server beside it is ready (docker.ProbeMount, docker.ProbeCommand). It asks the URL
// once and exits 0 for a 2xx answer and 1 for anything else, so the container needs no HTTP client
// of its own.
func probeCommand(ctx context.Context, d deps, args []string) int {
	if len(args) != 1 {
		_, _ = fmt.Fprintln(d.stderr, "usage: vibesys-cli probe-ready <url>")
		return exitInvalidInput
	}
	u, err := url.Parse(args[0])
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		_, _ = fmt.Fprintf(d.stderr, "probe-ready: %q is not an http URL\n", args[0])
		return exitInvalidInput
	}
	ctx, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return exitFailed
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return exitFailed
	}
	_ = resp.Body.Close()
	if resp.StatusCode/100 == 2 {
		return exitOK
	}
	return exitFailed
}
