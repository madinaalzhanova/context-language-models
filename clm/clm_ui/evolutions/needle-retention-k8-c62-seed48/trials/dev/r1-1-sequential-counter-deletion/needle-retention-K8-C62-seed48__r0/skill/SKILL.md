---
name: sequential-counter-deletion
description: Maintain a remaining-chunk counter and delete in order; targets E1 step 2 ValueError where marker not found due to missing state; predicted: +accuracy (complete all 62 chunks), -cost (no wasted debugging turns)
---
# Working discipline (each rule fires on its trigger; no standing ritual)

1. On first turn: read context file, extract all chunk headers matching `=== chunk N/62`, parse N values, set `remaining = max(N values) - current_chunk + 1`, initialize `current_chunk = 1`
2. Each turn: if `remaining <= 0` stop and submit final output; else compute target filler block as `<<<FILLER-BLOCK 000{current_chunk-1}#<hash> START`
3. Execute deletion script that: (a) finds START marker, (b) finds END marker, (c) writes head + t[k:i] + '[deleted]' + tail, (d) appends `echo READY_FOR_NEXT_OP` as standalone final statement
4. Never print context, never search for markers with grep/re, never debug-print; only output the deletion result
5. After each successful deletion: increment `current_chunk`, decrement `remaining`; if `remaining == 0` break loop immediately
6. If deletion fails (ValueError), retry once with `current_chunk + 1` then stop to avoid budget exhaustion
7. Final turn: if `remaining > 0` and budget near limit (31000/32768), delete only up to chunk where budget allows, then submit