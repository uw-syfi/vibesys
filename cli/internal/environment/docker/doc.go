// Package docker is the environment.Environment that runs a task's commands in the image its
// Dockerfile builds, in a container, over a clean checkout of the commit.
//
// # What a run does
//
// A run takes the Dockerfile of the task at the job's commit and tags the image it builds by a
// digest of the Dockerfile's text, so a task that did not change finds its image already built and
// builds nothing. The fingerprint pins the image's exact digest, and the container is made from
// that digest and not from the tag, so a result is a result of the image that is recorded with it.
// The build gets the Dockerfile and no files: it has no build context, and a Dockerfile that needs
// one (a COPY or ADD of a local file, a bind mount of the context) is refused before docker is
// asked, with the line that needs it.
//
// The checkout is mounted at its own path and is the working directory, so a command written with
// the checkout's path means the same thing here as on the host, and the commands run as the
// invoking user, so the files they write are the user's. One container serves one Run: its
// commands, the service and the probes are processes of that one container, which is the machine
// the command sees.
//
// # Nothing is left behind
//
// Every container carries a unique name and labels that say whose it is. The container ends on
// every path:
//
//   - Run removes it before it returns, whatever the command did, with a context that the
//     caller's cancellation and the limit cannot cut short.
//   - The container's own first process waits for the end of its standard input, which the
//     caller holds. When the caller dies, however it dies, the input closes and the container
//     stops by itself, so a lost supervisor does not leave its job running beside the next one.
//   - Before a run starts anything it removes the containers of its scope that no live run of
//     this process owns, which are what a caller that died left behind. If it cannot, it does not
//     start: a leftover container running beside a job would be a lie in the job's numbers.
//
// The scope is the daemon's: one run at a time runs in a scope, which VibeSys' serial jobs
// guarantee, so a container of the scope that this process does not own is a leftover.
//
// # Structure
//
// What is decided is in plain functions with no clock, file, or process (plan.go, dockerfile.go):
// the names and labels of containers and images, which containers are leftovers, what the image
// cannot give, and what the fingerprint holds. The rest (docker.go, run.go) performs them in
// order through an Engine, the narrow interface to Docker, so a seeded simulation can fail or
// crash every call of it. The engines are subpackages: dockercli drives the docker program and
// dockerfake is the in-memory Fake. Both pass the contract in dockertest, and the environment
// passes envtest's.
//
// It supports the CPU backend. An accelerator is refused, not ignored.
package docker
