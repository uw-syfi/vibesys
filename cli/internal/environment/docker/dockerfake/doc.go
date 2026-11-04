// Package dockerfake is a Docker daemon kept in memory: the docker.Engine the docker environment's
// tests run against, and the one a daemon's simulation can use where a real engine would make a
// test slow or flaky.
//
// A Daemon is the engine's side: its images and its containers, which outlive any one
// connection. A Client is one process's connection to it, which is what a docker environment
// holds. Their scripts run on fakeenv machines, so a container's commands are the scripts the
// fake environment already runs.
//
// What makes it more than a happy-path stand-in is what a test can do to it:
//
//   - Faults fails any call, before it does its work or after it (a reply that was lost), so a
//     test can show that nothing is left behind whichever call fails.
//   - Client.Crash ends the connection as the death of the process would: the containers the
//     client started stop, as the real engine's do when their input closes, and stay until a
//     later process removes them.
//   - Daemon.Plant adds a container nobody here started, and Daemon.Prune empties the images.
//
// The fake is held to the same engine contract as the real engine (dockertest), so a behavior a
// test relies on here is one the real engine has.
package dockerfake
