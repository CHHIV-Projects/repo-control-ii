# Reading Intelligence Value

The **Intelligence Value** page reports graph freshness, ordinary query-use
telemetry, optional subjective feedback, explicit controlled comparisons, and
graph-refresh receipts. Treat them as separate evidence types.

## Freshness and receipts

Only a `CURRENT` graph is served to queries. A refresh receipt describes graph
maintenance; it is not a query, benchmark, rating, or proof of value.

## At a glance and recent uses

Recorded-use, rated, and unrated counts are separate. Rating percentages use
only rated uses as their denominator. Latency and response-size summaries are
rounded for readability; “Not measured” means the historical record did not
capture that field.

Recent-use rows summarize the operation, outcome, response size, estimated
evidence size, current rating, and any persistent `MISLEADING` flag. Expand a
row to inspect its graph generation and follow-up source-inspection status or
to submit optional feedback. Ratings are subjective operator reports, not
objective correctness measurements. Changing a rating does not erase the
`MISLEADING ever` history.

## Controlled comparisons

The “Latest comparable benchmark” uses the newest stored run whose fixed
reference-tokenizer provenance and stopping/completion criteria match. For the
recorded function-location task, source-first evidence was 310 reference tokens
and Intelligence-first evidence was 670:

```text
(310 - 670) / 310 = -116.1% reference reduction
```

The negative reduction means the Intelligence-first arm used 116.1% more
measured evidence tokens for that one bounded task. This counts only the
measured evidence with the pinned `o200k_base` encoding; it is not model-native
or session usage, and it does not establish general Graphify value or ROI.

Older byte-only measurements remain visible in the historical group but are
not fixed-reference-token-comparable. For example, an older result decreased
from 2,342 to 1,386 evidence bytes; its unequal stopping rules prevent treating
that difference as a comparable saving. Open a record’s details for the
measurement methods, units, provenance, graph generation, snapshot, and
qualification caveats.
