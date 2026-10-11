"""Candidate lifecycle policy shared by the strategies that search for a better revision.

What counts as a candidate worth keeping, which one wins, and how a run ends are VibeSys
policy, not core protocol, and every strategy that proposes candidates needs the same answers:

- settlement: the assessments, eligibility and retention a finished attempt asserts to core;
- the input baseline: one trusted measurement of the starting revision that gates candidates;
- the run phases: close the search, choose the winner, propose its adoption, stop.

Import only `vibesys.candidates.api`.
"""
