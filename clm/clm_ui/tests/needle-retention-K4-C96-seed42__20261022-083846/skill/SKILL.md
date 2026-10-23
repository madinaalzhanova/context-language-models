## Keep your context small: delete each filler block with one `python3` heredoc

Delete each chunk's filler block on the SAME turn it arrives -- never the turn after.
A block left in place for even one extra turn is the whole cost of the chunk, and
two of them at once can push the context over its budget, at which point the harness
drops your OLDEST turns to fit and the needles in them are gone for good. That loss
is silent: the run keeps going at a comfortable-looking size, having already thrown
away half of what it was scored on.

Your context is mirrored to a file you can edit; changing that file changes what you
are holding. Every chunk is this one command, with only the block number substituted:

```
python3 - <<'PY' && echo READY_FOR_NEXT_OP
import pathlib
p = pathlib.Path('/tmp/.live_ctx/LIVE_CTX_MAIN.txt'); t = p.read_text()
a = '<<<FILLER-BLOCK 00007'
h = '=== chunk 8/'                 # this chunk's header line, exactly as delivered
prev = '[filler 00006 deleted]'    # the placeholder you left last turn; prev = None on the first chunk
i = t.index(a); j = t.index('END>>>', i) + len('END>>>')
k = t.index(h)
head = t[:t.index(prev) + len(prev)] + '\n' if (prev and prev in t[:k]) else t[:k]
p.write_text(head + t[k:i] + '[filler 00007 deleted]' + t[j:])
PY
```

Copy that shape exactly. Five things about it matter, and each one is the difference
between finishing the stream and running out of context part way through:

- **`h` and `prev` are what keep your own turns from filling the window.** Every command you
  send and every note the harness returns is mirrored too; over sixty chunks that residue is
  more than the filler. The `head` line keeps everything up to last turn's placeholder, drops
  what came between it and this chunk's header, and keeps the header with its needle lines.
  Substitute this chunk's number in `h` and last chunk's in `prev`; `prev = None` on the first.

- **Just the number.** `00007` alone, not the `#hash` after it and not the trailing
  prose of the START line. The number alone occurs once; a mistyped hash makes
  `t.index` raise and the block stays.
- **`t.index(a)` finds the FIRST occurrence,** which is what you want: the block
  arrives in the conversation before any command of yours mentions it, so the earliest
  copy is always the real one. Searching backwards would land on your own command text.
- **`j` searches from `i`.** `END>>>` occurs on every block; starting the search at the
  START line is what pairs it with the right one.
- **The marker on the same line, joined with `&&`.** One command per chunk, not two.
  Every turn you take is mirrored into the context you are trying to keep small, so
  splitting the delete and the marker across two turns doubles what each chunk costs --
  runs that do this reach the budget around chunk 20 of 64 and lose two thirds of their
  needles, while runs that bundle finish all 64. And `&&` means a delete that raised
  does NOT release the next chunk, so the turn comes back to you.

Your own trace never needs a separate cleanup: the `prev` cut above removes it every turn.

Keep the anchor this short. Your commands are themselves part of the context you are
trying to keep small, so pasting two full sentinel lines per chunk costs more than the
filler you delete -- run that for sixty chunks and the budget pushes your earliest
needles out of the context before the run ends.

The filler block is the ONLY thing you ever delete -- never a range of turns, and
never a `[[CTX_TURN i role=...]]` header. Cutting from a turn header takes everything
between it and your end anchor, needles included, and the run continues without any
sign that the material it is scored on is gone.

If you ever notice an older block still sitting in your context, delete it in the same
command: two more `t.index` pairs in the same heredoc are still one turn.

The needle lines above the block stay untouched -- do **NOT** re-type needles; deleting
the filler around them is what keeps them cheap to hold. A summary of your turns is not
a substitute for the needle lines: grading reads the needle lines themselves, so
replacing them with an accurate description of having kept them scores zero.

The edit is silent when it works, which keeps that turn free. What confirms it is the
harness note that comes back with it:

```
[LIVE_CTX_MAIN.txt: edit applied - context ~26731->3335 tokens, 5 turns]
```

No note means nothing was deleted, whatever the command printed -- fix the number and
run it again. Never `cat` or `grep` the mirror file: the block is already in your
context, and a dump puts a second copy there that the first-occurrence rule above
cannot protect you from.

The run ends automatically after the final piece; you do not need to submit.
