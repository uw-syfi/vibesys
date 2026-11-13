# Workload archetypes

Native command-line programs spend their time in different places. These
archetypes describe **behavioral traits of a workload**, so you can choose which
profiling questions to ask and which hypotheses are plausible. They are not
categories of executable: classify the measured invocation by what its profile
shows, not by the program's name. Real programs and real invocations usually
combine several (see [Mixed workloads](#mixed-workloads)).

Whatever the archetype, the rest of this skill applies unchanged: preserve the
whole observable contract, use the task's commands, rely on the task's recorded
measurement, and apply the [review checklist](review-checklist.md). The archetype
changes the questions, not the discipline.

## Streaming and text processing

Examples: grep-like search, sed-like and awk-like editing, byte-copy and filter
tools.

Traits: cost grows with input size; the program reads a stream and applies a
matching, parsing or transformation step to it.

Questions to ask:

- Is the per-byte or per-line loop the cost, or is it set-up (pattern or program
  compilation, locale and encoding set-up) that dominates on small inputs?
- How are bytes decoded: is a multibyte or locale-aware path taken where a simple
  one would give the same result, and is that path taken for every line?
- How does the matcher or interpreter work (automaton, backtracking, bytecode),
  and what does the scored pattern or script cost to prepare versus to run?
- How is input read: buffer sizes, number of `read` calls, copies between buffers,
  line splitting?
- How much is output, and how is it written (per-line writes, buffered)? A match
  that prints little is a different workload from one that prints most of its
  input.

Hazards: fast paths that assume ASCII or a simple pattern shape and silently
change results in other locales or on rare patterns.

## Filesystem traversal and metadata

Examples: ls-like listers, find-like walkers, du-like summarizers.

Traits: cost grows with the number of entries and with syscalls per entry, more
than with bytes.

Questions to ask:

- How many syscalls per entry (`open`, `readdir`, `stat` and relatives,
  `readlink`)? Which are required by the options in use, and which are made out of
  habit?
- How are paths built and compared: string allocation per entry, repeated
  normalization?
- Is output sorted, and with what comparison (locale collation is expensive)? Is
  the sort over names, over metadata, or both?
- What is formatted: permissions, owners (name lookups), times, sizes, colors,
  alignment and terminal width?
- What does the cache state look like (cold or warm directory and inode cache)?
  The timing and the answer to "is this CPU or I/O" depend on it.

Hazards: skipping metadata that some option or edge case still needs; ordering
that changes with locale or filesystem; mishandling symlinks, loops and
permission errors.

## Sorting, aggregation, comparison and formatting

Examples: sort-like, uniq-like, wc-like, diff-like and cut-like tools.

Traits: the program holds or compares data in memory, and cost depends on
comparison and data-structure choice, not just on reading.

Questions to ask:

- What does one comparison cost (locale-aware collation, numeric parsing per
  comparison, key extraction repeated)?
- How is data stored: allocation per record, pointer chasing, fragmentation, a
  temporary file when memory would do?
- What is the algorithmic shape (comparison sort, hashing, merge, an edit-distance
  search) and how does it scale with input size and with duplicates?
- How much of the time is formatting and writing the result?

Hazards: changing comparison or stability semantics; changing behavior on equal
keys, on invalid input, or under a different locale; memory growth on large input.

## Process and pipeline orchestration

Examples: xargs-like and tee-like tools, wrappers and supervisors.

Traits: much of the time is spent creating processes, moving data through pipes,
and waiting.

Questions to ask:

- How many processes are created, and could a batch be made larger or smaller
  without changing what each child receives?
- What is the cost per child (fork and exec, environment copying, argument
  construction, quoting)?
- Is the program waiting, and for what? CPU time may be small while wall time is
  large.
- How are pipes and buffers used, and what happens to data when a reader exits
  early?

Hazards: changed batching that alters command lines or limits, lost error
propagation, changed broken-pipe and signal behavior, ordering differences when
work is made concurrent.

## Stateful or repository-oriented tools

Examples: version-control tools, package and database clients, build tools; Git is
one instance.

Traits: the program discovers state on disk, reads indexes and objects, parses
configuration, may run subprocesses, and depends on the correctness of mutable
state.

Questions to ask:

- What is done once per invocation before any real work: repository discovery,
  configuration and attributes parsing, reading an index or manifest?
- How are stored objects located and read, and are they decoded once or many
  times?
- Does the invocation traverse the working tree, and how much of that is
  avoidable by information already in the index?
- Which subprocesses are spawned, and how much of their cost is set-up?
- What caches exist, who owns them, and what invalidates them?

Hazards: stale caches that return wrong answers after the state changes; reading
state in a different order than the original; concurrent invocations and
locking; behavior across different repository shapes (empty, large, damaged).
Tests should vary the state, not only the command.

## Mixed workloads

A real invocation usually combines archetypes, and its profile decides the mix:

- A **recursive text search** combines traversal and metadata (archetype 2) with
  scanning and matching (archetype 1). On a tree of many small files the traversal
  and open calls may cost more than the scan; on a few large files, the reverse.
- A **long-format directory listing** combines traversal, metadata syscalls,
  sorting with locale-aware comparison, name and time lookups and formatting
  (archetypes 2 and 3).
- A **search across a repository** may combine repository discovery and object
  access (archetype 5), traversal (2), matching (1) and subprocess set-up (4).

So: profile the invocation, assign its cost to archetypes by evidence, and take the
questions from the archetypes that carry the time. Do not assume that because a
program is "a search tool" its cost is in matching, or that a "lister" is mostly
I/O. Re-classify as input size, option sets or cache state change.

## Using the archetypes

They are prompts for questions and a vocabulary for records, nothing more. They do
not prescribe an optimization, and a technique that suits one program can be
irrelevant or harmful for another. If a kind of program turns out to need
substantially different correctness criteria, profiling techniques, build
mechanisms or review rules, that is a sign it may deserve its own guidance; until
then, this skill and these questions are the shared foundation.
