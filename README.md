# PACU Scheduler

## Operator policy notes

The scheduler currently applies the following intentional policies:

- **Weekend history is canonical.** Any weekend assignment edit (`add_assignment`, `modify_assignment`, `remove_assignment`, or `restore`) rewrites derived state (`weekend_rotation_history`, violation dates/stats, in-memory last patterns) from `weekend_assignments` to keep assignments/patterns/violations synchronized.  
- **Manual violation overrides are temporary by design.** `set_violation_count` writes an operator override directly, and that override remains in place until an explicit rebuild/recompute action is run (`_recalculate_violation_counts` / “Rebuild Violation History”).  
- **Weekend generation mode default is strict-then-relaxed.** `generate_schedule(..., weekend_variant_mode=STRICT_THEN_RELAXED)` first attempts strict alternation; if no variants survive, it falls back to the relaxed branch. `RELAXED_ALLOWED` skips strict generation and uses relaxed generation immediately.
- **Conflict handling for pre-scheduled weekends is hard-blocking.** Candidate FSF/SFS pairs are rejected when they contradict any non-empty prefilled Friday/Saturday/Sunday cell.

These policies are intentionally conservative to prioritize schedule consistency and operator control.
