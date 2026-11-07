# Clients architecture

The desktop app presents runs owned by host services. It owns connections, local
preferences and views; it does not own the run or infer facts from narration.
[Architecture](architecture.md) describes those process boundaries. The
[Python module graph](python-module-graph.md) covers the older engine.

## The package graph

The client workspace contains the desktop app, a component gallery, generated
host and orchestrator bindings, a display-name catalog and the design system.
The app composes those libraries. The gallery uses the design system and catalog
without importing the app.

A package owns one concept and exposes declared entrypoints. A helper used by
one app stays in that app. New packages need a second consumer, generated code
or a measured build problem to justify the extra boundary.

### Layers

Dependencies point strictly down the declared package layers. Peers cannot
import one another, so apps compose their dependencies. Foundation libraries
have no workspace dependencies. The intermediate layers remain available for
shared pure logic, domain logic, services and features when consumers need them.

The package graph checker validates declared layers, folder placement, exports
and dependency edges. Compiler project references mirror workspace dependencies.
No undeclared import or relative path into another package is allowed.

### Entry points and runtimes

Each entrypoint declares the runtime capabilities it needs. Neutral entries use
no browser or Node APIs. Renderer entries have browser capabilities. Main-process
entries have Node capabilities. The Electron preload loads only Electron at
runtime and carries workspace types only when compilation erases them.

The renderer reaches host I/O through a typed bridge to the main process. It
cannot import a Node implementation. Compiler libraries, dependency checks and
inspection of the emitted preload enforce these limits.

## The Effect desktop app

The app is one package with separate roles for pure state, services, views,
the main process, the preload and test implementations. Those boundaries give
it isolation without turning each role into a package.

The main process owns process creation, SSH links, local and remote host
provisioning, connections and calls. The preload transfers typed bytes across
the Electron boundary. The renderer composes services and displays their state.
An SSH authentication failure is reported for the operator to fix in a terminal.

Inside each role folder the code is grouped by feature: the shell (rail and
navigation), the settings and the orchestrator each have a subfolder in `state`,
`core`, `ui` and `testing` where they have something to put there. A feature's
fold is a reducer, its I/O is a service, and its screens only display. The
shell composes the others: it owns where the person is, and every screen sends
a navigation intent that the shell's fold takes. A dependency rule keeps the
features from importing one another, and a test holds each declared intent to a
control that sends it.

## State

Control state is Effect services and atoms. Data folds are plain reducers.

A service expresses a role with interchangeable real and Fake implementations.
Entrypoints assemble the implementations. Work shared by a host belongs to
that host's scope; work for a view belongs to the view's scope. Closing the owner
interrupts its work and releases its resources.

Subscriptions, timers and child processes have bounded lifetimes. Reconnecting
a host or replacing a view retires the old work before accepting its successor's
results. Cancellation and connection policy live in the service layer rather
than being repeated in component callbacks.

Reducers turn typed snapshots and changes into immutable data. They read no
clock, start no work and import no Effect services. Display text is never an
input to a control decision. A full state replaces the previous state only at
its committed boundary; ordered changes apply after it.

### Link liveness

How long a phase may take and whether the link is alive are different questions, answered
separately. Each phase of dialing a host has the bound that fits what it waits for: bringing
the ssh link up and short commands by the clock, the copy of the host program by its progress
(a chunk not accepted within a stall window ends it, a slow copy that keeps moving never is),
and the handshake once the program runs. Nothing bounds a dial as a whole, so a slow first
connection finishes. An open session is alive while bytes arrive from the host, however slowly.
A session silent for a window is probed with the handshake; one silent through a second window
is lost and redialed, so a hung link is noticed within the silence bound (`state/liveness.ts`).

A call is held to the same rule. A request, and a stream until its first reply, is stalled when a
whole window (`REPLY_STALL_MS`) passed with nothing heard on it, so a reply that keeps arriving,
however slowly and however large, is never cut, and one that stops is cut within two windows. The
window has no wall deadline of its own on a call; only a command's receipt keeps one. A host's
connection outlives its last call by a short linger (`CONNECTION_LINGER_MS`), so the handshake and
the feed that follows share one dial and one ssh master. A master that freezes and ignores its
control socket is killed by the pid it reported (once that pid is still an ssh master), and the
host is redialed on a fresh one; a teardown that cannot be confirmed refuses one lease, never the
host for good.

The end of a call separates a lost link from a refusal. A stream that ends with no gRPC status,
a closed socket and an ssh exit are a lost link, which the reconnect policy redials. A status is
the host's own word and is shown as such, never retried. So is what the app refuses itself, typed
across the port (`DesktopRefusal`): a method it does not allow, a request that breaks its schema, a
platform no host program is built for, a build that holds none for the host's platform (the message
names the platform and the version, which is the API hash of the program). And so is what the host
says about why it does not serve: after an UNAVAILABLE handshake the connection asks `Identify`, and
a state or configuration the host refused is shown with its cause and the setting that fixes it
(a state directory on a local disk), not retried; a host merely held by another process is waited
out. The connection exposes each host's freshness as data (live, reconnecting or stale, and when
the host last spoke); screens show it and compute nothing from the clock.

Dependency rules keep pure state free of effects, services free of the renderer
and main process, and the renderer free of main-process and preload internals.
Lint rules restrict ambient time, randomness, Promise APIs and execution of
Effect programs to their owning boundaries.

## Design tokens and themes

The design system owns components, tokens, styles, fonts and themes. Each fact
has one source of truth; generated styles derive from typed token tables.
Theme colors use semantic roles, so components ask for a role rather than
choosing a literal color. Missing roles fail type checking.

Themes follow the operating system until the operator chooses one. The
operator's choice (a mode, and a light and a dark theme) is a protobuf message
the main process keeps in the app's user data; the list of themes is read from
the token table, so a theme is added by a token block and an enum value with no
change to a screen. A theme change crossfades and the first theme shown does
not. Reduced motion and contrast requirements belong to the design system, not
to each app. Fonts ship with the app rather than being fetched while a screen
opens.

Motion has one scale. Durations are `fast` (a hover, a press, a selection),
`base` (a part of a screen arriving) and `slow` (a whole screen arriving, a theme
crossfade), with `instant` for what must not move; easings are `easing` between
two states, `easing-enter` for something arriving and `easing-exit` for something
leaving. Motion marks a change of meaning and never loops. Under reduced motion
every duration is zero.

## Design system enforcement

Apps build views from public design-system components and tokens. They ship no
stylesheets and introduce no raw interactive elements, colors or lengths.
The design system has no domain dependencies; a component's reusable behavior
must make sense outside the run model.

Package checks restrict imports and stylesheet ownership. Lint rules reject raw
elements and values in apps and raw values in the design system outside its
token definitions. A custom-property check catches references to missing tokens.
Accessibility, information hierarchy and whether a component belongs in the
system still need human review.

## Components

The design system owns component behavior and public component contracts.
Its [README](https://github.com/uw-syfi/vibesys/blob/main/clients/libs/ui/README.md)
describes the available components. App screens compose these primitives with
run data; domain-specific components stay in the app until reuse justifies a
shared owner.

### Toasts and inline status

Transient feedback uses toasts. A condition that changes what an operator can
do stays beside the affected control or view. Failures name the action and
leave enough context for the operator to decide what to do next.

### The gallery and its screenshots

The gallery exercises components across themes and states independently of the
app. Screenshot comparisons protect their appearance. Desktop fixtures exercise
complete screens through the same services used by the live app, with Fakes at
external boundaries. Component screenshots and screen fixtures answer different
questions; neither substitutes for the other.

## Where a desktop test goes

Pure folds and connection policy are tested without I/O. Effect service tests
use a virtual clock and seeded randomness. Fakes replace process, network,
clock, identity and filesystem boundaries; the layers above them run normally.
A test advances simulated time and asserts state, so its verdict never depends
on elapsed wall time. The runner's bound guards only against hangs.

Real-system checks cover the built Electron app and real host connections.
They do not justify moving real time or sockets into deterministic tests.
The repository testing skills own test placement and commands.

## Display names

The Python catalog owns names for the older engine's roles. Clients consume a
generated table rather than copying it. Machine identifiers stay stable while
the catalog supplies the words a person sees. Protocol facts come from generated
service bindings, not from Python-generated application types.

## Build model

Workspace packages expose source to the bundler and declarations to the type
checker. Project references and build caching follow the dependency graph.
Generated bindings and token styles are checked against their authoritative
inputs. Apps own executable bundles; libraries remain reusable dependencies.

The package checker, dependency rules, type checker, lint and unused-code checks
cover different boundaries. Run the repository client checks together before a
push, as described in the [development guide](development.md#client-checks-before-a-push).
See [Desktop app development](desktop-development.md) for the development entrypoint.
