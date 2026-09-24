# CDC recovery contract

This capture comes from a MySQL 8.0 `sales.orders` migration incident. GTID mode and row-based logging are enabled, with `binlog_row_image=MINIMAL`. The connector checkpoint was written on the original source. Authority later moved through promoted replicas before the recorded stop coordinate.

The dataset is synthetic, but the semantics model production GTID failover, InnoDB crash recovery, TABLE_MAP lifetime, online schema migration, XA, partial JSON updates, and partition exchange maintenance.

## Capture records

`/app/data/evidence_codec.py` decodes the primitive binary records. It exposes valid collector events, row-image values, page copies, undo records, transaction-history records, redo blocks, and redo operations. It does not assemble server history, maintain page state, select a redo history, decide visibility, or produce the CDC feed.

Collector directory names and fragment order are not chronological evidence. A binlog coordinate is local to one `source_uuid`. For a source, `(binlog_file, start_pos, end_pos)` identifies one event. Valid duplicate observations at that coordinate are the same event; conflicting valid observations would make the capture invalid. An event without `source_uuid` belongs to `checkpoint_source_uuid`.

TABLE_MAP bindings are source-local. `ROTATE` and source handoff clear them.

## GTID authority

A GTID denotes one transaction even when copies appear on several servers. `PREVIOUS_GTIDS` records the inherited executed set of that server at that point; file positions from different servers are never compared as one timeline.

Before a promoted server begins its own GTIDs, its last `PREVIOUS_GTIDS` record defines the predecessor history it inherited. A predecessor transaction outside that set is stranded and has no effect on the authoritative catalog, row state, cutover, or output.

A promoted server may contain a replicated copy of an inherited predecessor GTID. Such a copy is evidence about the same transaction, not another commit. Partial valid copies of the same GTID can contribute non-conflicting row-event observations while preserving their transaction order. The origin server's commit event remains the transaction's `commit_file` and `commit_pos`.

A collector may also contain a passive downstream replica that originated no GTIDs of its own. That stream is evidence about replicated transactions, not another authority handoff. When several valid copies of one GTID are incomplete, each observed row-event sequence is an ordered subsequence of the original transaction. No copy has precedence merely because it is the origin or because its local binlog position is later. The recovered row-event order must preserve every valid copy; this capture is constructed so those constraints determine one total row-event order. If more than one order survived all copies, the evidence would be insufficient rather than something to resolve with a local tie-break.

A later promotion can merge two diverged histories. In that case the promoted server's pre-native binlog prefix may contain `log_slave_updates` copies from more than one origin SID before the server emits its first locally originated GTID. Those copied transactions are the order in which the merge server made the selected branch transactions durable. A branch transaction is authoritative only if its GTID is present in the merge server's inherited `PREVIOUS_GTIDS` frontier; a locally committed branch transaction omitted from that frontier is stranded. The merge server's durable prefix order defines the state entering its first native transaction and the recovered feed order for those merged transactions.

The merge copy does not replace origin identity. Row events for a merged GTID can be reconstructed from all non-conflicting copies of that GTID, but `commit_file` and `commit_pos` still come from the origin server's `XID` or `XA_COMMIT`. A damaged merge-server copy can therefore establish durable cross-branch order without being sufficient by itself to reconstruct every row change.

## Catalog and TABLE_MAP semantics

`catalog_history.jsonl` is authoritative for the checkpoint source. Its intervals use that source's coordinates. A checkpoint-source TABLE_MAP must match the active catalog interval at the event coordinate.

A promoted server inherits the committed physical objects, logical table names, and column order present at its inherited GTID frontier. Later committed DDL on the authoritative server changes that state.

The cutover is the committed statement:

`RENAME TABLE orders TO orders_archive, _orders_new TO orders`

After that commit, the old object is `orders_archive` and the shadow object is logical `sales.orders`.

Physical ordinal matters. `MODIFY COLUMN ... AFTER ...` changes ordinal even when the SQL type does not change. The capture also adds and drops an invisible `route_bucket BIGINT`, changes `note` from `VARCHAR` to `JSON`, and later reorders `amount_cents` and `tax_cents`. Invisible columns participate in TABLE_MAP ordinals and row-image bitmaps but are absent from the six-column canonical output. Existing rows receive `route_bucket = 0` when it is added. The `VARCHAR` to `JSON` conversion preserves an existing SQL string as a JSON string scalar.

A row event uses the TABLE_MAP and catalog layout that existed when the event was written. A prepared XA transaction keeps the row-image interpretation established before `XA_PREPARE`; later DDL does not reinterpret its earlier row events.

## Row-state semantics

The physical type codes used here are signed 64-bit integer (`8`), signed 16-bit integer (`2`), UTF-8 string (`253`), and normalized JSON (`245`).

MINIMAL UPDATE and DELETE images can omit unchanged non-key columns. The complete `before` row is the transaction-visible row immediately before that operation; the complete `after` row is the row immediately after it. Earlier operations in the same transaction therefore affect the base state of later operations.

Every explicit column in a `before` image is an observation of that same transaction-visible row. It must agree with the recovered base state. An inconsistency invalidates that historical interpretation; an explicit image is not a separate overwrite layer.

`PARTIAL_UPDATE_ROWS` has the same base-state rule. Ordinary after-image values apply before `json_diffs`. JSON diffs then apply in listed order. Their column number is the physical ordinal under that event's TABLE_MAP. `replace` requires an existing target, `remove` requires an existing target, and `insert` requires a missing object member or a valid array insertion position. Earlier JSON operations affect later operations.

A primary-key-changing UPDATE is one logical update. Its `before.id` is the old key and `after.id` is the new key; later operations in that transaction address the new key.

## Checkpoint storage semantics

The two JSONL snapshots are page-scan exports from the crashed source. Rows on pages named by `snapshots/mvcc_manifest.json` are governed by the page, redo, undo, transaction-history, and read-view evidence rather than by the JSONL value alone.

For a recovery page, a usable starting copy has a valid checksum and `page_lsn <= capture_lsn`. If several usable copies exist, the one with the greatest page LSN is the starting copy.

`redo_manifest.json` defines the required sequence range, starting LSN, base previous CRC, and recovery LSN. A physically valid redo history has exactly one checksum-valid block for every required sequence; its first block starts at the required LSN; adjacent blocks are LSN-contiguous and linked by `prev_crc32`; and the last block ends at `recovery_lsn`. Blocks can come from different mirrors. More than one physically valid history may exist.

Redo mini-transactions are atomic. Reusing the same mini-transaction id in one candidate history is invalid. A slot patch changes only its listed fields and row-head metadata. A page reorganization changes slot order; later slot numbers refer to the reorganized order.

The saved read view uses these visibility rules: `trx_id < up_limit_id` is visible; `trx_id >= low_limit_id` is not visible; between the bounds, a transaction active at `view_lsn` is not visible and another transaction is visible. Undo links describe the predecessor versions. A visible delete-marked version means the row is absent.

The authoritative checkpoint history is the unique history whose recovered row state is consistent with all authoritative storage and later row observations in the capture. No mirror, branch position, or recency preference is authoritative by itself.

## Transaction durability

A normal GTID transaction becomes durable at `XID`.

For XA, `XA_PREPARE` is not a commit. Prepared changes become durable only at the matching `XA_COMMIT`; `XA_ROLLBACK` discards them. Prepared state survives `ROTATE`, while TABLE_MAP bindings do not. A row event inside a prepared XA keeps the physical backing and logical table role that were in force when that row event was written. Later DDL, including partition EXCHANGE, does not retarget that prepared row to a different backing and does not relabel an `orders` row as staging work or vice versa. At `XA_COMMIT` its overlay is applied to that captured backing; `XA_ROLLBACK` discards it. Feed inclusion is based on the captured logical role of the row event, not the backing's role at commit time.

Commit order is durability order, not GTID number order. A prepared XA can therefore commit after newer normal transactions.

`delivered_gtids` is the connector's inclusive executed set at the saved checkpoint. Those GTIDs are not emitted again.

## Migration and partition semantics

Before cutover, `sales.orders` is physical object `orders-v1-6c9a` and `sales._orders_new` is `orders-v2-21df`. `migration.sql` defines the old-to-shadow transformation, and trigger effects are part of the same transaction as the old-table DML.

Before cutover, one logical user change is the old-table operation paired with its corresponding shadow-table effect. The canonical emitted row comes from the shadow object. Shadow-only migration maintenance can change reconstruction state but is not a separate user change. No user transaction spans the committed rename. After cutover, logical `sales.orders` row changes are emitted directly. Cleanup against `orders_archive` is not emitted.

The tail creates `sales.orders_stage`, partitions `sales.orders` at `id = 15000`, and uses `EXCHANGE PARTITION p_hot WITH TABLE orders_stage WITHOUT VALIDATION`. `storage_objects.json` gives the initial physical backings.

`CREATE TABLE orders_stage LIKE orders` creates an empty table with the current physical layout. Row events while a backing is serving the staging role mutate that backing but are not user-feed changes.

EXCHANGE atomically swaps the physical backing assigned to `p_hot` with the backing assigned to `orders_stage`. It does not copy rows and creates no synthetic row events. A later exchange can put a previously staged backing back under logical `sales.orders` with all mutations it accumulated while staged. A live TABLE_MAP for `orders` or `orders_stage` continues to describe that logical table and its row layout after EXCHANGE; EXCHANGE does not clear the binding. For each later row event, resolve the physical backing from the table's role at that event, rather than freezing the backing that happened to be assigned when the TABLE_MAP was first observed.

A partitioned UPDATE may change the primary key across the range boundary. The `before` row is read from the backing selected by `before.id`; the `after` row is written to the backing selected by `after.id`. This is still one logical update, not a delete plus insert. Later operations in the same transaction address the new key and therefore the new backing.

For an emitted change, `source_object_id` is the physical backing that supplies the resulting canonical row version: for insert/update it is the destination backing selected by the after key; for delete it is the source backing selected by the before key. Before partitioning, post-cutover changes use `orders-v2-21df`; after partitioning, the cold range uses the cold/root backing and the hot range uses the backing assigned to `p_hot` at that transaction.

## Output semantics

The recovery interval begins after the saved checkpoint and ends at the stop coordinate on `stop_source_uuid`.

The canonical row has the six columns listed in `connector_checkpoint.json`. Every non-null `before` or `after` contains all six. Inserts have `before = null`; deletes have `after = null`; updates have both.

`transactions` contains every undelivered authoritative committed transaction that emitted at least one logical user change, in durability order. For a normal transaction, `commit_file` is the origin-source `XID` event's `binlog_file`, and `commit_pos` is that event's `end_pos` (never its `start_pos`). For XA, `commit_file` is the origin-source `XA_COMMIT` event's `binlog_file`, and `commit_pos` is that event's `end_pos`; `XA_PREPARE` does not define the commit coordinate. A replicated copy never replaces the origin commit event. The `cutover` object uses the committed rename transaction's origin-source `XID` `binlog_file` and `end_pos`.

`mode` is `dual_write` for non-XA pre-cutover user changes, `xa_dual_write` for XA pre-cutover user changes, and `direct_v2` for post-cutover logical `sales.orders` changes, including normal and XA work after partitioning. Transactions with no logical user changes are omitted.

`changes` follows transaction order and then logical user-operation order within each transaction after migration-only and staging-role work is suppressed. `ordinal` is 1-based within the emitted changes of its transaction. `pk` is `before.id` for delete and `after.id` for insert/update. `schema_epoch` is `pre_cutover` before the committed rename and `post_cutover` after it. `source_object_id` follows the physical-backing rule above.

`cutover` records the rename GTID, origin commit coordinate, and old/new physical object ids. `recovered_through` is the checkpoint's recorded stop file and position.
