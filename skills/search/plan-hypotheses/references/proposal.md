# Proposal section

The `## Proposal` section of your plan file. Keep each field to a line or two. The header (three
single lines) and the other two sections are described in `vibesys:state`.

```
status: proposed
claim: <one sentence a measurement can falsify>
gain: 

## Proposal
Claim: <the same sentence>
Evidence: <the measurement or bound it rests on: file, number, command, record id>
Mechanism: <what in the code costs time today, with file:line>
Expected gain: <metric change as a range, with the arithmetic: share of time touched x fraction
  removed, capped by the headroom of that part>. Say "unattributed" if the share is not measured.
Confidence: <low | medium | high>, and why. Mark bets.
Files touched: <so the lead can check that chosen plans do not overlap>
Order: <must follow or precede which other plan, if they share code>
Rule risk: <numerics, delegation, benchmark tuning, memory budget; or "none found">
How to test: <the smallest change or measurement that reaches a result in one worker pass, and
  the cheap checks that need no measurement slot>
Rejected alternatives: <idea> - <the evidence that rules it out>

## Decision

## Result
```

Typical weak spots: a gain larger than the headroom, a time share taken from a guess, a
configured load used in place of the observed one, and two plans that edit the same function
without saying which goes first.
