---
name: minimal-direct-deletion
description: Direct index-based deletion without marker search or debugging; targets E1 steps 3-5 where agent wasted turns on prints; predicted: -cost (fewer turns), +accuracy (no failed searches)
---
# Working discipline (each rule fires on its trigger; no standing ritual)

1. On first turn: read context file, find all `=== chunk N/62` headers, extract N values, sort ascending, set `chunk_list = sorted(N values)`
2. Each turn: if `len(chunk_list) == 0` stop and submit; else `target_chunk = chunk_list[0]`, remove from list
3. Deletion script: (a) find `=== chunk {target_chunk}/62` position, (b) find next `=== chunk {target_chunk+1}/62` or END, (c) extract and write head + filler-deletion + tail, (d) append `echo READY_FOR_NEXT_OP` as standalone final statement
4. Never search for `<<<FILLER-BLOCK` markers; use chunk header positions directly
5. Never print context, never grep, never debug-print; only output deletion result
6. If deletion fails: skip to next chunk in list, do not retry same chunk
7. Final turn: submit output regardless of remaining chunks if budget exhausted