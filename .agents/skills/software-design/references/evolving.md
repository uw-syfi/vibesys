# Evolving existing code

Read this when you refactor, split a module, migrate callers, or change a
contract. Language-specific tools are in [python.md](python.md) and
[typescript.md](typescript.md).

## Two hats, separate commits

Wear one hat at a time. A refactor commit changes structure and no behavior; a
feature or fix commit changes behavior. Never mix them in one commit, so each
can be reviewed and reverted alone.

Prepare first: if the change is hard in the current structure, refactor to make
it easy, then make the change. Keep the refactor to the code on your change's
own path. Put it in an earlier commit, or an earlier PR when it is large.

## Splitting a large module

1. **Find the seam.** Look for a group of behaviors that share data and change
   together, and that callers use as one thing.
2. **Define the interface first.** Write down the operations, their contract,
   and their failure modes before moving code. It should be smaller than the
   code behind it.
3. **Declare it.** Register the new unit with the dependency tooling and expose
   only that interface, so the boundary is enforced from the first commit.
4. **Move the implementation** behind the interface with no behavior change.
   Keep tests green at every step.
5. **Repoint callers** to the interface, then remove the old access path.

If callers must change in step 5, do it as expand, migrate, contract (below).

## Changing a contract

For a signature, public interface, schema, or wire format:

1. **Expand.** Add the new form beside the old. Both work.
2. **Migrate.** Move every caller to the new form, in reviewable steps.
3. **Contract.** Remove the old form. This step must land; a change with only
   the first two steps is unfinished.

Prefer additive changes. When a change cannot be additive, make the
compatibility boundary explicit and version it.

## Migration scaffolding

Anything temporary (a shim, an adapter, both code paths, a flag) needs an owner
and a removal condition, written where the next reader will find it. If you
cannot say when it goes away, do not add it.

## When to stop

Refactors invite rabbit holes. If cleanup outside your path is needed, stop and
file an issue instead of doing it. Do not extend a violating pattern to save
effort; use the seam or the registry that already exists, or add one.

## Recording deferred drift

In the PR's `Design` section, list each violation you touched but did not fix,
the reason, and the issue tracking it. If your change grows a known violation
(for example, a file already over its size limit), state why that was
unavoidable.
