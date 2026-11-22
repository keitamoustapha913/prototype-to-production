# Operations queue redesign

The queue helps a small operations team understand incoming work and hand
its current workload to the evening team. The implementation is a Python
command-line tool. Keep its existing CSV input, Python standard-library
dependency policy and existing no-argument report. No service, browser UI,
database, or authentication changes are in scope.

## Prioritization workstream

Operators need to list unresolved items in priority order. Add a command-line
option named `--prioritized` for that view. Keep open and active rows, omit closed rows, then sort
by urgent, normal and low priority. When priorities tie, keep input order.
Unknown priorities follow low priority, and their original text remains
visible. A queue without unresolved items prints a plain-language empty
message. Existing reports keep their current behavior.

## Export workstream

Add a separate `--export-csv` option for the same ordered unresolved items so the evening
team can open the handoff in a spreadsheet. Write only CSV to stdout, preserve
the column order id, title, status, priority, and preserve punctuation in
titles. An empty handoff produces zero bytes. Missing or malformed input
must report the problem to stderr and exit unsuccessfully. A second command
reading the handoff should see the same unresolved items and order as the
operator report.

Treat prioritization and export as separate workstreams and implementation
slices. Export depends on the shared prioritization behavior. Include
acceptance examples for tied priority, unknown priority, no unresolved work,
titles containing commas, and malformed input. Preserve the sample queue.

## File ownership and compatibility

Prioritization owns `queue_report.py` and handles command routing, input
validation, shared ordering, and the operator report. Export owns a new
`queue_export.py`, which writes the validated, ordered rows as CSV. Load
the writer only when the export route is selected so the original report
and operator view can run after the first slice. The original report
still handles no arguments and all argument lists other than the two
new single-flag invocations. Routing is an earlier step within the
Prioritization slice. Export depends on that slice.
