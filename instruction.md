# Orders CDC recovery

We had a gap in the `sales.orders` feed while the v2 table was being cut over. I put the material we still have under `/app/data`: three partial binlog captures, the connector checkpoint, catalog history, the migration SQL, and the two checkpoint snapshots.

Please reconstruct what the connector should have delivered after its saved checkpoint, through the stop coordinate recorded in that checkpoint. `/app/data/recovery_contract.md` is the authority for interpreting the capture. The fragment order and filenames are just how the evidence was collected; use the recorded binlog coordinates and historical catalog state when deciding what an event means.

Write the recovered feed to `/app/output/recovered_cdc.json`. Another tool consumes this file, so the JSON shape is fixed:

- `recovered_through` has `file` and `pos` for the recovery stop coordinate.
- `cutover` has `gtid`, `commit_file`, `commit_pos`, `old_object_id`, and `new_object_id` for the committed rename/cutover transaction.
- `transactions` contains each recovered transaction that produced at least one logical user change. Each item has `gtid`, `commit_file`, `commit_pos`, `mode`, and `change_count`. The allowed modes are `xa_dual_write`, `dual_write`, and `direct_v2`.
- `changes` contains the logical row changes. Each item has `gtid`, `ordinal`, `op`, `pk`, `before`, `after`, `source_object_id`, and `schema_epoch`. `op` is `insert`, `update`, or `delete`; `schema_epoch` is `pre_cutover` or `post_cutover`.

Whenever `before` or `after` is present, return the full six-column canonical row described by the checkpoint. Use JSON `null` for the missing side of an insert or delete.

Keep transactions in commit order. Within a transaction, keep the logical row changes in their original order. JSON object-key order is irrelevant.

I only need the logical user feed. Migration-only writes to the shadow table and cleanup on the archived table should affect reconstruction where the contract says they do, but they are not separate downstream user changes. Likewise, do not assume the table name visible at the end of the capture was the identity of an older row event.

You have 7200 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
