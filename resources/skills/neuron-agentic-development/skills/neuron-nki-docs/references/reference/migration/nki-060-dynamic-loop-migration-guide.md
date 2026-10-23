# NKI 0.6.0 Dynamic Loop Migration Guide

Migrate on-device dynamic loops from `for i in nl.dynamic_range(...)` and bare
`while reg:` to the structured constructs `nl.fori_loop` and `nl.while_loop`.

## Why migrate

The NKI frontend is moving from **Parsing** to **Tracing**:

| Frontend | Status |
|----------|--------|
| Parsing (legacy) | current default |
| Tracing | available in NKI **0.6.0**, default in **0.7.0**, parser removed in **0.8.0** |

Tracing removes support for `for i in nl.dynamic_range(...)` and bare
`while reg:`. These forms build an on-device loop from a runtime register; under
the tracer, Python executes once to build the graph, so a register — which has
**no value at build time** — cannot be used in Python control flow (`for`/`while`)
or via `int()`/`range()`. Doing so raises `TypeError`. The syntax cannot be
preserved, so this is a **breaking change**.

Both replacements (`nl.fori_loop`, `nl.while_loop`) also compile on the parser,
so a migrated kernel runs on **both** frontends. That is what makes the switch
safe and lets you fall back to the parser during the transition
(`NKI_COMPILER_FRONTEND=parser`) if needed.

## Do I need to migrate?

| Construct | Migrate? |
|-----------|----------|
| `for i in nl.dynamic_range(...)` | **Yes** |
| bare `while reg:` (register condition) | **Yes** |
| `nl.affine_range` / `nl.sequential_range` / `nl.static_range` | No — compile-time loops, unaffected |
| `range(...)` over a Python int | No |

Only loops whose bound or condition is a **runtime hardware register** are
affected.

## Pattern 1 — counted loop with a runtime bound

`nl.dynamic_range(reg)` → `nl.fori_loop(lower, upper, body_fun, step=1)`. The
loop body becomes a callable that receives the iteration value as a
`VirtualRegister`. The closure captures enclosing variables by reference
(standard Python LEGB scoping).

**Before (parser-only, removed under tracing):**
```python
for i in nl.dynamic_range(reg):
    nisa.dma_copy(dst=temp, src=data.ap(scalar_offset=i, indirect_dim=1))
```

**After (parser + tracer):**
```python
def body(i):
    nisa.dma_copy(dst=temp, src=data.ap(scalar_offset=i, indirect_dim=1))

nl.fori_loop(0, reg, body)
```

`fori_loop(lower, upper, body_fun, step=1)` iterates `lower, lower+step, ...`
up to (but not including) `upper`, exactly like `range(lower, upper, step)`.

## Pattern 2 — data-dependent loop (while)

Bare `while reg:` → `nl.while_loop(init, body_fun)`. The body returns the **next**
condition register. It is a true `while` (skips the body entirely if `init` is
zero), not a do-while.

**Before (parser-only, removed under tracing):**
```python
while reg:
    nisa.tensor_tensor(dst=acc, data1=acc, data2=val, op=nl.add)
    nisa.tensor_scalar(dst=count_sb, data=count_sb, op0=nl.add, operand0=-1)
    nisa.register_load(reg, count_sb)
```

**After (parser + tracer):**
```python
def body(r):
    nisa.tensor_tensor(dst=acc, data1=acc, data2=val, op=nl.add)
    nisa.tensor_scalar(dst=count_sb, data=count_sb, op0=nl.add, operand0=-1)
    nisa.register_load(r, count_sb)
    return r

nl.while_loop(reg, body)
```

## Mechanical transform recipe

The conversion is mechanical (as applied across 15 loops / 10 kernels in the
nkilib refactor):

```
for VAR in nl.dynamic_range(LB, UB):
    BODY
```
becomes
```
def _fori_body_N(VAR):
    BODY
nl.fori_loop(LB, UB, _fori_body_N)
```

The inner function closes over enclosing variables by reference, so tensors and
constants defined before the loop remain accessible inside `BODY` unchanged.
Give each body a unique name (`_fori_body_0`, `_fori_body_1`, ...) when several
appear in one kernel.

## Rules for migrated loops

- **Keep cross-iteration state in SBUF or HBM.** Carry running values
  (accumulators, counters, running max/sum) in SBUF/HBM and update them in place,
  rather than passing them between iterations.
- **The loop variable is read-only.** Do not write to it or perform Python
  arithmetic on it. Read it as a runtime offset via `.ap(scalar_offset=...)`.
- **The loop step must be a positive compile-time integer.** `nl.fori_loop` only
  counts up.
- **The `while_loop` body can only return the condition for the next iteration.**
  `nl.while_loop` continues while that register is non-zero.
- **The `fori_loop` body function cannot return a value.**
- **Tensors (`nl.ndarray`) declared inside a body function cannot be referenced
  outside of the loop.** Any tensor that needs to be used outside of the body
  function must be declared outside the body. The body is a separate MLIR region;
  allocate outputs before the loop and write inside, while PSUM tiles used only
  within an iteration are created inside.
- **These loops cannot sit inside an `nl.no_reorder()` block.** Static loops
  (`affine_range` / `sequential_range` / `static_range`) are allowed there;
  `fori_loop` / `while_loop` are not.
- **Use unique op names inside the body** (the body is traced once).

## Migration checklist

1. Grep for `dynamic_range` and register-conditioned `while`.
2. Convert to `nl.fori_loop` / `nl.while_loop` with named callable bodies.
3. Move cross-iteration state to SBUF/HBM; allocate outputs before the loop, PSUM
   inside.
4. For LNC2 kernels, confirm both cores iterate the same count.
5. Select the frontend at runtime with the `NKI_COMPILER_FRONTEND` env var
   (`parser` | `tracer`) — no code change needed. Run your suite with
   `NKI_COMPILER_FRONTEND=tracer` and confirm parity with the parser.

## API reference

- `nl.fori_loop` and `nl.while_loop` signatures and examples:
  [nki.language dimensions & loops](../../programming/api/api-nki-language-dims.md#nki-language-fori_loop)
- Writing-side guidance: the `/neuron-nki-writing` skill's
  `references/nki-language-constraint.md` (structured-dynamic-loops section).
