"""Templates the serial hypothesis-search strategies share.

`plan.j2` is the new-hypothesis planning request; it reads the fields of `PlanEvidence` plus the
run-level variables (`objective`, `environment_notes`), and renders the protocol includes from
`vibesys.prompts.CORE_PROTOCOL_DIR`, which a strategy lists as a fallback root beside this one.
"""
