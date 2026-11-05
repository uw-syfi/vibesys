// Package docker is the environment.Environment that runs a task's commands in the image its
// Dockerfile builds, in a container, over a clean checkout of the commit.
//
// # What a run does
//
// A run takes the files of the task's directory at the job's commit as the build context, with the
// Dockerfile at its root, and tags the image it builds by a digest of the context and of the
// engine's local image of each base the Dockerfile builds on, so a task that did not change finds
// its image already built and builds nothing, and a base rebuilt under the same tag builds it again. The build sees the task's files
// and no file of the candidate. The fingerprint pins the image's exact digest, the machine and the
// GPUs, and the container is made from that digest and not from the tag, so a result is a result
// of the image that is recorded with it.
//
// The processes that build on one engine, of every daemon that reaches its machine, build one
// image once: a build takes the tag's lease in Config.Builds, and one that finds it held waits for
// it and uses what the holder built. A build that fails while the engine then has an image under
// the tag lost a race with a builder that took no lease there, and uses that image.
//
// Besides the checkout, a container gets the cache directory of the task's image (CacheDir), an
// empty artifacts directory when the caller gives one (ArtifactsDir), and what each resource of
// its placement attaches (internal/resource): GPUs, a cluster's kubeconfig and network, read-only
// directories. A task that needs a resource the placement does not give is refused. A Machine says
// where these come from: the engine may run on another machine, and then a run's directories are
// copied there and back.
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
// One run at a time runs in a scope, which the caller guarantees: the daemon gives a scope to each
// job's runs on a machine where it runs several jobs at once, and to itself on one where it runs
// one at a time. So a container of the scope that this process does not own is a leftover.
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
// It supports the CPU backend, and the CUDA backend when the placement gives GPUs. Any other
// accelerator is refused, not ignored.
package docker
