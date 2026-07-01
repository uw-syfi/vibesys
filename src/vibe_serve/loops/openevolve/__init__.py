"""OpenEvolve-style outer loop: MAP-Elites archive + cell-uniform selection.

A thin sibling of :mod:`vibe_serve.loops.evolve` that swaps the flat
fitness-weighted population for a behavioral-feature-binned
:class:`MapElitesArchive`. Each iteration samples a cell uniformly,
draws the cell's elite as parent, mutates, evaluates, and re-bins.

Reuses ``evolve``'s mutator / judge / profiler helpers and templates
verbatim — only selection diverges.
"""
