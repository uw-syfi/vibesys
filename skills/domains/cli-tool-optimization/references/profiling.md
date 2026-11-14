# Profiling

Profile the invocations the task actually scores, before changing code. A
profile answers where the time goes; it does not say what to change, and it is
not the score.

## Choose what to profile

1. List the scored invocations and how often each occurs. Weight each the way the
   score does, not by how interesting it is: by share of total time if the score
   sums time, but equally per invocation if the score is a geometric mean of
   per-invocation ratios (then many short invocations matter as much as a few
   long ones, and start-up cost is paid by every one of them).
2. Cluster similar invocations (same options, similar input size). Profile one
   representative per cluster that carries a meaningful share, plus the single
   most expensive one.
3. Reproduce each exactly: arguments, working directory, environment variables,
   locale, input files, and where output goes. A terminal, a pipe and a file can
   take different code paths. Also match inherited process state: signal
   dispositions (an ignored `SIGPIPE` turns death by signal into a write error),
   umask, open descriptors, resource limits, and whether standard output and
   standard error share one pipe.
4. Prefer the evaluator's own way of running one recorded invocation. An evaluator
   that replays invocations in an isolated root runs them against its own libraries,
   locale data and file layout; a hand-made reproduction on the host profiles
   different code. If you reproduce by hand, say how your reproduction differs.

## Tools, when available and permitted

Use what the task or the environment provides; an unavailable tool is a reason to
use another, not to skip profiling. Containers and restricted hosts often block
some of these, in particular hardware counters (`perf`). Where they are blocked,
callgrind's instruction counts and `strace` counts are the usual substitutes, with
system time and page faults taken from the process's resource usage. Run profilers
only where the task allows load, never during a measurement.

| Tool | Good for | Watch for |
| --- | --- | --- |
| `perf stat`, `perf record`, `perf report` (Linux) | Where CPU time goes; hardware counters (instructions, cycles, cache and branch misses) | Needs kernel permission; short runs give few samples; kernel symbols may be hidden |
| `sample` or Instruments (macOS) | Where CPU time goes on macOS | Sampling interval limits short runs |
| callgrind (Valgrind) | Exact instruction counts per function, including the dynamic loader; steady even for short runs; good for before/after comparison | Slow; reports instructions, not time (caches are simulated only with `--cache-sim=yes`); sees user space only, so `execve`, page faults and `mmap` work in the kernel are invisible |
| `strace -c`, `strace -f -tt -T`, `ltrace` (Linux), `dtruss` (macOS) | Syscall counts, order and latency; library call counts; subprocesses | Tracing slows the program and changes timing; use it to count, not to time. `ltrace` often fails on binaries linked with immediate binding; `dtruss` needs System Integrity Protection relaxed |
| `/usr/bin/time -v` or equivalent | User, system and wall time, peak memory, page faults | Coarse |
| Allocation profilers (`heaptrack`, `massif`, allocator statistics) | Allocation counts, sizes, lifetimes | Overhead; results depend on the allocator |
| Compiler instrumentation (`-pg`, `-fprofile-generate`, `-finstrument-functions`) | Call counts and graphs when sampling is too coarse | Changes the build; keep it separate from the measured binary |
| Loader diagnostics (`LD_DEBUG=statistics`) | Dynamic-loading and relocation time | Linux and glibc specific |

## Sampling versus tracing

**Sampling** interrupts the program periodically and records where it was. It has
low overhead and shows where time is spent, but it needs enough run time to
collect samples: for a process that lasts milliseconds, profile many launches
together, or raise the sampling rate, or use counts from callgrind instead.
**Tracing** and **instrumentation** record every event, so counts are exact, but
they perturb timing. Use sampling to find where time goes and tracing to count
events (syscalls, allocations, calls). Read self time (time in the function) and
inclusive time (with callees) separately, and remember that inlining moves cost
between functions.

Build with debug symbols (`-g`) for readable output; this does not change code
generation in normal toolchains. Distribution-style builds strip the shipped
executable: the symbols are in the build tree or a separate debug package, so
profile the same build with its symbols, and take library symbols from the system's
debug packages. Frame-pointer or DWARF call graphs may need a separate build or a
recording option. Do not time the profiling build.

## Separate start-up from steady state

Run the same program on trivial input (an empty file, a no-op option, `--version`
where it does the same set-up) and on representative input. The first number is
the fixed floor; the difference is the per-input work. Check each of:

- process creation and dynamic linking (how many libraries, how many relocations),
- locale and internationalization initialization,
- configuration, environment and startup file reading,
- argument and option parsing,
- allocator and runtime initialization,
- teardown and flushing at exit.

Then compare two input sizes to see what scales linearly, what scales worse, and
what does not scale at all.

## Where per-input time goes

Work through the costs that apply to this program, and say which are present:

- **Scanning and matching**: byte loops, decoding, pattern or expression
  compilation (once per run or once per input line?), regular-expression engine.
- **Parsing**: tokenizing options, input formats, configuration, data records.
- **Traversal**: directory walking, `open`, `stat` and related metadata calls,
  path construction, symlink handling.
- **Sorting and aggregation**: comparison cost, collation, allocation of keys,
  temporary storage, data structure choice.
- **Formatting**: converting numbers, times and names to text; locale-dependent
  output; terminal width and color decisions.
- **I/O**: read and write sizes, buffering, number of syscalls per byte or per
  file, output volume.
- **Allocation**: count and size of allocations, reallocations, page faults.
- **Process creation**: forks, execs, pipes, waits.

Attribute each cost center to code you may edit or to a library you may not (the C
library's regular expressions, locale and character-set conversion data, a pattern
library). For a library, the levers are calling it less, calling it differently, or
an in-tree alternative where the objective allows one. Reimplementing a library's
behavior byte for byte is a large semantic risk; bound it before funding it.

## Classify the workload

Use measurements, not impressions:

| Class | Typical evidence |
| --- | --- |
| CPU-bound | CPU time close to wall time; time concentrated in user-space functions |
| Syscall-bound | High system time; syscall count large relative to work; time in kernel entry. Page faults and `execve`/`mmap` work also show up as system time |
| I/O-bound | Wall time well above CPU time; changes with cache state; waiting on reads or writes |
| Allocation-bound | Allocator functions and page faults prominent; count of allocations scales with input |
| Start-up dominated | Time on trivial input is close to time on representative input |

A workload can be more than one class, and the class can change with input size.
Record which class applies to each profiled cluster.

## From profile to hypothesis

Name the dominant cost center and its share of the measured time. The share is the
ceiling for any change to it. State a hypothesis in measurable terms, then run the
smallest change that tests it. If the profile does not point at a single dominant
center, profile with a finer split before changing code.
