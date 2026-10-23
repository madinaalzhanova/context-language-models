---
name: budget-aware-sequential
description: Delete chunks sequentially while tracking token budget; targets E1 step 38/39 where agent hit budget with remaining blocks; predicted: -cost (stop early when budget critical), +accuracy (complete more chunks before timeout)
---
# Working discipline (each rule fires on its trigger; no standing ritual)

1. On first turn: read context file, count total filler blocks via regex `<<<FILLER-BLOCK (\d+)`, set `total_chunks = count`, set `current_chunk = 1`
2. Each turn: estimate tokens per chunk from first deletion (measure head+tail size), compute `max_chunks_remaining = (32768 - current_context_tokens) / avg_chunk_tokens`
3. If `current_chunk > max_chunks_remaining` AND `current_chunk < total_chunks`: delete only up to `max_chunks_remaining` chunks, then submit immediately
4. Deletion script must: (a) find exact START/END markers for current chunk, (b) preserve all non-filler content, (c) append `echo READY_FOR_NEXT_OP` as standalone final statement
5. Never run grep, never print context, never search for markers with regex; use direct string index operations
6. After each deletion: increment `current_chunk`, update `current_context_tokens` from file size
7. If `current_chunk == total_chunks` or budget exhausted: submit final output without waiting for READY_FOR_NEXT_OP
