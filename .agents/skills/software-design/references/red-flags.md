# Red flags

A shared vocabulary for the design checkpoint and for review. Each flag is a
prompt to investigate, not an automatic failure: confirm the symptom, then
decide whether the fix is worth it in this change (see
[evolving.md](evolving.md)).

## Module shape

| Flag | Symptom | Usual fix |
| --- | --- | --- |
| Shallow module | The interface is about as large as the implementation; callers do the real work | Merge it into its caller, or move more behavior behind it |
| Information leakage | Two modules encode the same design decision (a format, an ordering, a name) | Move the decision into one module and expose a narrow operation |
| Pass-through method | A method only forwards to another with the same or similar signature | Delete the layer, or give it a different abstraction |
| Conjoined methods | You cannot understand one method without reading another | Merge them, or split along a real subtask with a simple interface |
| Overexposure | A common use needs callers to learn rare features or options | Default the rare cases; hide options behind the common path |
| Temporal decomposition | Structure follows the order things run in, so one decision is spread across steps | Group by the knowledge each unit holds, not by when it runs |
| Special-general mixture | General-purpose code contains special-case code for one caller | Move the special case out to that caller |

## Clarity

| Flag | Symptom | Usual fix |
| --- | --- | --- |
| Repetition | The same logic in several places, or the same fact stated twice | Extract the mechanism; if it is a fact, give it one source |
| Hard to describe | The contract takes a long paragraph or needs "and also" | The design is muddled; redesign before documenting |
| Vague name | A name that could mean many things, or a name that was hard to pick | Fix the abstraction, then the name |

## Change patterns

| Flag | Symptom | Usual fix |
| --- | --- | --- |
| Shotgun surgery | One logical change edits many modules | Gather the scattered decision into one module |
| Divergent change | One module changes for several unrelated reasons | Split it by reason to change |
| Feature envy | A function mostly uses another module's data | Move it to the module that owns the data |
| Data clump | The same few values travel together through signatures | Introduce a named type for them |
| Primitive obsession | Raw strings or numbers carry domain meaning | Use an enum or a small value type |

## Forced substitution

These mean unlike things were put behind one interface (rule 3):

- Methods that some implementations skip, no-op, or reject.
- `supports_x` flags, or kind and type checks in callers.
- A parameter or method only one implementation uses.
- The interface grows every time an implementation is added.
- A catch-all context or options bag as an argument.
- A shared contract test that needs per-implementation skips.

Fix in this order: narrow role interfaces; a closed union with exhaustive
matching; optional capability interfaces; adapters at the wiring layer;
duplicate until the third case; extract only the common mechanism.
