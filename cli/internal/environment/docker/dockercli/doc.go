// Package dockercli is the docker.Engine that drives the docker program. It is the only place the
// docker environment touches a real Docker engine, and it holds the facts about that engine the
// environment's decisions rest on:
//
//   - A container is kept up by a first process that waits for the end of its standard input,
//     which the caller holds through the pipe of `docker run -i`. When the caller dies, however
//     it dies, the pipe closes, the first process ends, and the container stops by itself. The
//     container is not made with --rm: the caller removes it, and a container that stopped and
//     was not removed is what the next run's sweep finds.
//   - A command runs with `docker exec` through a small shell that merges its standard error
//     into its standard output inside the container, so the two come back as one ordered stream
//     and anything `docker exec` itself writes to standard error is the engine's, not the
//     command's. A program that does not exist ends with 127, as in any shell.
//   - A command killed by a signal exits with 128 plus the signal, and so does a command whose
//     container was removed under it. The engine tells the two apart by asking whether the
//     container still runs, and reports the second as lost.
//   - `docker exec` does not stop its command when the client dies. The commands of a container
//     end when the container is removed, which the environment does on every path.
//
// Every call is bounded by the injected timers, so that an engine that does not answer fails the
// call instead of hanging the run. A build is not: it takes as long as it takes, and only the
// caller's context ends it.
package dockercli
