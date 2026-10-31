# Idea template

One block per idea, in rank order. Ids are stable (`<letter><number>`), so the lead can refer to
them. Keep each field to a line or two.

```
Id / title:
Claim: <one sentence a measurement can falsify>
Evidence: <the measurement or bound it rests on: file, number, command>
Mechanism: <what in the code costs time today, with file:line>
Gain: <metric change as a range, with the arithmetic: share of time touched x fraction removed,
  capped by the headroom of that part>. Say "unattributed" if the share is not measured.
Confidence: <low | medium | high>, and why. Mark bets.
Files touched: <so the lead can check that picked ideas do not overlap>
Order: <must follow or precede which other idea, if they share code>
Rule risk: <numerics, delegation, benchmark tuning, memory budget; or "none found">
Cheap checks: <what can be checked without a measurement slot>
First step: <the smallest change or measurement that reaches a result in one worker pass>
```

Rejected block:

```
Rejected: <idea> - <the evidence that rules it out>
```

Typical weak spots: a gain larger than the headroom, a time share taken from a guess, a
configured load used in place of the observed one, and two ideas that edit the same function
without saying which goes first.
