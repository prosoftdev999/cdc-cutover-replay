from __future__ import annotations
import copy, json, re
from pathlib import Path
from collections import defaultdict, deque
from frames import event_coord, file_no, gtid_in_set, parse_gtid_set
from rowcodec import decode_image
from mvcc_checkpoint import recover_checkpoint_candidates
OBJ1 = 'orders-v1-6c9a'
OBJ2 = 'orders-v2-21df'
TYPE = {'id': 8, 'customer_id': 8, 'amount_cents': 8, 'status_code': 2, 'status': 253, 'note': 253, 'tax_cents': 8, 'route_bucket': 8}

def coord_tuple(v):
    return (file_no(v['file']), int(v['pos']))

def load_jsonl(p):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

def col_types(cols):
    return [TYPE[c] for c in cols]

def sql_type_code(sql_type):
    t = sql_type.upper()
    if t.startswith('BIGINT'):
        return 8
    if t.startswith('SMALLINT'):
        return 2
    if t.startswith('VARCHAR'):
        return 253
    if t.startswith('JSON'):
        return 245
    raise ValueError(f'unsupported SQL type {sql_type}')

def catalog_at(catalog, database, table, coord):
    hits = []
    for r in catalog:
        if r['database'] != database or r['table'] != table:
            continue
        start = coord_tuple(r['effective_from'])
        end = coord_tuple(r['effective_to']) if r['effective_to'] else None
        if start <= coord and (end is None or coord < end):
            hits.append(r)
    if len(hits) != 1:
        raise ValueError(f'catalog match {database}.{table} at {coord}: {len(hits)}')
    return hits[0]

def active_catalog(catalog, coord, database='sales'):
    out = {}
    for r in catalog:
        if r['database'] != database:
            continue
        start = coord_tuple(r['effective_from'])
        end = coord_tuple(r['effective_to']) if r['effective_to'] else None
        if start <= coord and (end is None or coord < end):
            out[r['table']] = {'object_id': r['object_id'], 'columns': [c['name'] for c in r['columns']], 'types': [int(c['type']) for c in r['columns']]}
    return out

def snapshot(path, cols):
    return {int(r['id']): {c: r.get(c) for c in cols} for r in load_jsonl(path)}

def op_name(t):
    return {'WRITE_ROWS': 'insert', 'UPDATE_ROWS': 'update', 'PARTIAL_UPDATE_ROWS': 'update', 'DELETE_ROWS': 'delete'}[t]

def move_after(cols, name, after):
    out = list(cols)
    out.remove(name)
    out.insert(out.index(after) + 1, name)
    return out

def gtid_sid(gtid):
    return gtid.rsplit(':', 1)[0]

def json_path_tokens(path):
    if not path.startswith('$'):
        raise ValueError(f'invalid JSON path {path}')
    out = []
    i = 1
    while i < len(path):
        if path[i] == '.':
            i += 1
            j = i
            while j < len(path) and (path[j].isalnum() or path[j] == '_'):
                j += 1
            if j == i:
                raise ValueError(f'invalid JSON member path {path}')
            out.append(('key', path[i:j]))
            i = j
        elif path[i] == '[':
            j = path.find(']', i + 1)
            if j < 0:
                raise ValueError(f'unclosed JSON index {path}')
            out.append(('idx', int(path[i + 1:j])))
            i = j + 1
        else:
            raise ValueError(f'invalid JSON path {path}')
    return out

def apply_json_ops(value, ops):
    doc = copy.deepcopy(value)
    for item in ops:
        op = item['op']
        toks = json_path_tokens(item['path'])
        if not toks:
            if op == 'remove':
                doc = None
            elif op in ('insert', 'replace'):
                doc = copy.deepcopy(item.get('value'))
            else:
                raise ValueError(f'bad JSON diff op {op}')
            continue
        parent = doc
        for kind, key in toks[:-1]:
            parent = parent[key] if kind == 'key' else parent[key]
        kind, key = toks[-1]
        if kind == 'key':
            if not isinstance(parent, dict):
                raise ValueError('JSON path parent is not an object')
            exists = key in parent
            if op == 'replace':
                if not exists:
                    raise ValueError('replace target missing')
                parent[key] = copy.deepcopy(item['value'])
            elif op == 'insert':
                if exists:
                    raise ValueError('insert target already exists')
                parent[key] = copy.deepcopy(item['value'])
            elif op == 'remove':
                if not exists:
                    raise ValueError('remove target missing')
                del parent[key]
            else:
                raise ValueError(f'bad JSON diff op {op}')
        else:
            if not isinstance(parent, list):
                raise ValueError('JSON path parent is not an array')
            if op == 'replace':
                if key < 0 or key >= len(parent):
                    raise ValueError('replace index out of range')
                parent[key] = copy.deepcopy(item['value'])
            elif op == 'insert':
                if key < 0 or key > len(parent):
                    raise ValueError('insert index out of range')
                parent.insert(key, copy.deepcopy(item['value']))
            elif op == 'remove':
                if key < 0 or key >= len(parent):
                    raise ValueError('remove index out of range')
                parent.pop(key)
            else:
                raise ValueError(f'bad JSON diff op {op}')
    return doc

def tx_copies(events):
    """Return row-bearing transaction copies keyed by GTID.

    The capture can hold a predecessor transaction again in a promoted replica's
    binlog.  Only the semantic row-event sequence is needed for copy repair; the
    origin source still supplies the transaction's commit coordinate.
    """
    out = {}
    cur = None
    maps = {}
    for e in events:
        et = e['event_type']
        if et == 'ROTATE':
            maps = {}
        elif et == 'TABLE_MAP':
            maps[int(e['table_id'])] = {'database': e['database'], 'table': e['table'], 'types': list(e['column_types'])}
        elif et == 'GTID':
            cur = e['gtid']
            out[cur] = []
        elif cur and et in ('WRITE_ROWS', 'UPDATE_ROWS', 'PARTIAL_UPDATE_ROWS', 'DELETE_ROWS'):
            m = maps.get(int(e['table_id']))
            if m is None:
                raise ValueError(f'missing TABLE_MAP in copy {cur}')
            out[cur].append({'database': m['database'], 'table': m['table'], 'types': m['types'], 'event_type': et, 'rows': copy.deepcopy(e['rows'])})
        elif cur and et in ('XID', 'XA_PREPARE'):
            cur = None
    return out

def token(d):
    return json.dumps(d, sort_keys=True, separators=(',', ':'))

def scs(a, b):
    """Shortest common supersequence for two partial ordered observations."""
    A = [token(x) for x in a]
    B = [token(x) for x in b]
    n, m = (len(A), len(B))
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n, -1, -1):
        for j in range(m, -1, -1):
            if i == n:
                dp[i][j] = m - j
            elif j == m:
                dp[i][j] = n - i
            elif A[i] == B[j]:
                dp[i][j] = 1 + dp[i + 1][j + 1]
            else:
                dp[i][j] = 1 + min(dp[i + 1][j], dp[i][j + 1])
    i = j = 0
    out = []
    while i < n or j < m:
        if i == n:
            out.extend(copy.deepcopy(b[j:]))
            break
        if j == m:
            out.extend(copy.deepcopy(a[i:]))
            break
        if A[i] == B[j]:
            out.append(copy.deepcopy(a[i]))
            i += 1
            j += 1
        elif dp[i + 1][j] <= dp[i][j + 1]:
            out.append(copy.deepcopy(a[i]))
            i += 1
        else:
            out.append(copy.deepcopy(b[j]))
            j += 1

    def is_sub(seq, whole):
        it = iter([token(x) for x in whole])
        return all((any((y == token(x) for y in it)) for x in seq))
    if not is_sub(a, out) or not is_sub(b, out):
        raise ValueError('transaction-copy merge failed')
    return out

def merge_partial_orders(copies):
    """Merge several incomplete observations of one transaction.

    Each capture copy is an ordered subsequence of the original row-event
    sequence.  For the transactions that need this path the semantic row
    events are distinct, so their union forms a small precedence graph.  A
    usable recovery must have one unique topological order; otherwise the
    evidence is insufficient rather than something we tie-break locally.
    """
    reps = {}
    edges = defaultdict(set)
    indegree = defaultdict(int)
    for seq in copies:
        toks = [token(x) for x in seq]
        if len(set(toks)) != len(toks):
            raise ValueError('multi-copy merge requires distinct semantic row events')
        for item, tok in zip(seq, toks):
            reps.setdefault(tok, copy.deepcopy(item))
            indegree.setdefault(tok, 0)
        for a, b in zip(toks, toks[1:]):
            if b not in edges[a]:
                edges[a].add(b)
                indegree[b] += 1
    ready = sorted([t for t in reps if indegree[t] == 0])
    ordered = []
    while ready:
        if len(ready) != 1:
            raise ValueError('partial transaction copies do not determine one row-event order')
        cur = ready.pop()
        ordered.append(reps[cur])
        for nxt in sorted(edges.get(cur, ())):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if len(ordered) != len(reps):
        raise ValueError('partial transaction copies contain an ordering cycle')
    return ordered

def initial_physical_candidates(root: Path):
    checkpoint = json.loads((root / 'connector_checkpoint.json').read_text())
    v1_columns = ['id', 'customer_id', 'amount_cents', 'status_code', 'note']
    v2_columns = checkpoint['canonical_columns']
    physical = {OBJ1: snapshot(root / 'snapshots/orders_v1.jsonl', v1_columns), OBJ2: snapshot(root / 'snapshots/orders_v2.jsonl', v2_columns)}
    layouts = {OBJ1: (v1_columns, col_types(v1_columns)), OBJ2: (v2_columns, col_types(v2_columns))}
    return recover_checkpoint_candidates(root, physical, layouts)

class Engine:

    def __init__(self, root: Path, streams, physical=None):
        self.root = root
        self.streams = streams
        self.checkpoint = json.loads((root / 'connector_checkpoint.json').read_text())
        self.catalog_history = load_jsonl(root / 'catalog_history.jsonl')
        storage_path = root / 'storage_objects.json'
        self.storage_objects = json.loads(storage_path.read_text()) if storage_path.exists() else {'orders_root_object_id': OBJ2, 'p_cold_object_id': OBJ2, 'p_hot_object_id': 'orders-v2-hot-7a11', 'orders_stage_object_id': 'orders-stage-44c1'}
        if self.storage_objects.get('orders_root_object_id') != OBJ2:
            raise ValueError('storage object metadata disagrees with tracked orders root')
        self.checkpoint_source = self.checkpoint['checkpoint_source_uuid']
        self.stop_source = self.checkpoint['stop_source_uuid']
        self.stop_coord = coord_tuple(self.checkpoint['stop'])
        self.delivered = parse_gtid_set(self.checkpoint['delivered_gtids'])
        self.v1_columns = ['id', 'customer_id', 'amount_cents', 'status_code', 'note']
        self.v2_columns = self.checkpoint['canonical_columns']
        if physical is None:
            candidates = initial_physical_candidates(root)
            if len(candidates) != 1:
                raise ValueError(f'checkpoint has {len(candidates)} coherent crash-local states; later evidence is required')
            physical = candidates[0]
        self.physical = copy.deepcopy(physical)
        self.catalog = None
        self.table_maps = {}
        self.current = None
        self.prepared = {}
        self.committed = []
        self.cutover = None
        self.commit_counter = 0
        self.partitioned = False
        self.partition_boundary = None
        self.partition_backing = {}
        self.stage_backing = None

    def previous_records(self, source):
        return [e for e in self.streams[source] if e['event_type'] == 'PREVIOUS_GTIDS' and e.get('gtid_set')]

    def source_native_info(self, source):
        """Return the first locally originated GTID index and the inherited frontier before it."""
        events = self.streams[source]
        native = next((i for i, e in enumerate(events) if e['event_type'] == 'GTID' and gtid_sid(e['gtid']) == source), None)
        if native is None:
            raise ValueError(f'{source}: no locally originated GTID')
        prev = [(i, e) for i, e in enumerate(events[:native + 1]) if e['event_type'] == 'PREVIOUS_GTIDS' and e.get('gtid_set')]
        if not prev:
            raise ValueError(f'{source}: no PREVIOUS_GTIDS before local history')
        i, e = prev[-1]
        return (native, parse_gtid_set(e['gtid_set']))

    def promotion_chain(self):
        """Order sources by the inherited-source set visible before each source's local history."""
        # Collector evidence can include passive replicas that never originated
        # a GTID of their own.  They are useful transaction witnesses, not
        # promotion steps.
        known = {self.checkpoint_source}
        for source, events in self.streams.items():
            if source == self.checkpoint_source:
                continue
            if any(e.get('event_type') == 'GTID' and gtid_sid(e.get('gtid', '')) == source for e in events):
                known.add(source)
        ranked = [(0, self.checkpoint_source)]
        for source in known - {self.checkpoint_source}:
            _, frontier = self.source_native_info(source)
            ancestors = {sid for sid in frontier if sid in known and sid != source}
            ranked.append((len(ancestors), source))
        ranked.sort()
        ranks = [r for r, _ in ranked]
        if ranks != list(range(len(ranked))):
            raise ValueError(f'promotion lineage is not a single chain: {ranked}')
        chain = [s for _, s in ranked]
        if chain[-1] != self.stop_source:
            raise ValueError(f'stop source is not the terminal promotion: {chain}')
        for pred, succ in zip(chain, chain[1:]):
            _, frontier = self.source_native_info(succ)
            if pred not in frontier:
                raise ValueError(f'{succ}: inherited frontier omits predecessor {pred}')
        return chain

    def linear_chain_to(self, target, excluded=()):
        """Recover the ordinary single-predecessor lineage up to target."""
        excluded = set(excluded)
        known = {self.checkpoint_source}
        for source, events in self.streams.items():
            if source in excluded or source == self.checkpoint_source:
                continue
            if any(e.get('event_type') == 'GTID' and gtid_sid(e.get('gtid', '')) == source for e in events):
                known.add(source)
        if target not in known:
            raise ValueError(f'lineage target is not a native source: {target}')
        ranked = [(0, self.checkpoint_source)]
        for source in known - {self.checkpoint_source}:
            _, frontier = self.source_native_info(source)
            ancestors = {sid for sid in frontier if sid in known and sid != source}
            ranked.append((len(ancestors), source))
        ranked.sort()
        chain = [source for _, source in ranked]
        if chain[-1] != target or [rank for rank, _ in ranked] != list(range(len(ranked))):
            raise ValueError(f'lineage to {target} is not a single chain: {ranked}')
        return chain

    def merge_handoff_info(self):
        """Detect a terminal promotion that merged two diverged GTID histories."""
        final = self.stop_source
        try:
            final_start, final_frontier = self.source_native_info(final)
        except ValueError:
            return None
        prefix = self.streams[final][:final_start]
        origins = []
        for e in prefix:
            if e.get('event_type') != 'GTID':
                continue
            sid = gtid_sid(e['gtid'])
            if sid == final or not gtid_in_set(e['gtid'], final_frontier):
                continue
            if sid not in origins:
                origins.append(sid)
        if len(origins) < 2:
            return None
        pairs = []
        for side in origins:
            try:
                _, side_frontier = self.source_native_info(side)
            except ValueError:
                continue
            for base in origins:
                if base != side and base in side_frontier:
                    pairs.append((base, side, final, final_start, final_frontier, side_frontier))
        if len(pairs) != 1:
            raise ValueError(f'merge handoff is not uniquely identifiable: origins={origins}, pairs={[(a,b) for a,b,*_ in pairs]}')
        return pairs[0]

    def origin_commit_events(self, source):
        """Map locally originated normal GTIDs to their origin XID event."""
        out = {}
        current = None
        for e in self.streams[source]:
            et = e.get('event_type')
            if et == 'GTID':
                current = e['gtid'] if gtid_sid(e['gtid']) == source else None
            elif et == 'XID' and current is not None:
                out[current] = e
                current = None
            elif et in ('XA_PREPARE', 'XA_ROLLBACK'):
                current = None
        return out

    def init_catalog_from_checkpoint_source(self):
        if not self.committed:
            raise ValueError('no inherited checkpoint-source commit')
        self.catalog = active_catalog(self.catalog_history, self.committed[-1]['commit_coord'])
        if 'orders' not in self.catalog or '_orders_new' not in self.catalog:
            raise ValueError('migration catalog not active at first promotion')

    def bind_map(self, e, source):
        if source == self.checkpoint_source:
            r = catalog_at(self.catalog_history, e['database'], e['table'], event_coord(e))
            obj = r['object_id']
            cols = [c['name'] for c in r['columns']]
            types = [c['type'] for c in r['columns']]
        else:
            r = self.catalog.get(e['table']) if self.catalog else None
            if r is None:
                raise ValueError(f"no catalog state for {e['table']} on {source}")
            obj = r['object_id']
            cols = list(r['columns'])
            types = list(r['types'])
        if types != list(e['column_types']):
            raise ValueError(f"TABLE_MAP type mismatch for {e['table']}")
        self.table_maps[int(e['table_id'])] = {'object_id': obj, 'columns': cols, 'types': types, 'table': e['table']}

    def start_tx(self, gtid, source, override=None):
        self.current = {'gtid': gtid, 'source_uuid': source, 'changes': [], 'overlay': {}, 'xa': False, 'ddl': [], 'override': override}

    def read_tx_row(self, obj, pk):
        key = (obj, pk)
        if key in self.current['overlay']:
            v = self.current['overlay'][key]
            return None if v is None else dict(v)
        v = self.physical[obj].get(pk)
        return None if v is None else dict(v)

    @staticmethod
    def apply_partial(base, part, cols):
        out = {} if base is None else dict(base)
        for i, v in part.items():
            out[cols[i]] = v
        return out

    def apply_row(self, obj, cols, types, event_type, rows, logical_table=None):
        for rr in rows:
            bp = decode_image(types, rr['before_bitmap'], rr['before_data']) if 'before_data' in rr else None
            ap = decode_image(types, rr['after_bitmap'], rr['after_data']) if 'after_data' in rr else None
            if event_type == 'WRITE_ROWS':
                if ap is None or 0 not in ap:
                    raise ValueError('insert missing primary key')
                before = None
                after = self.apply_partial(None, ap, cols)
                old = None
                new = int(after['id'])
            else:
                if bp is None or 0 not in bp:
                    raise ValueError('before image missing primary key')
                old = int(bp[0])
                base = self.read_tx_row(obj, old)
                if base is None:
                    raise ValueError(f'no visible base {obj}:{old}')
                for ordinal, value in bp.items():
                    name = cols[ordinal]
                    if base.get(name) != value:
                        raise ValueError(f'before-image disagrees with reconstructed state for {obj}:{old}:{name}')
                before = dict(base)
                if event_type == 'DELETE_ROWS':
                    after = None
                    new = None
                else:
                    after = self.apply_partial(before, ap or {}, cols)
                    if event_type == 'PARTIAL_UPDATE_ROWS':
                        for diff in rr.get('json_diffs', []):
                            ordinal = int(diff['ordinal'])
                            if ordinal < 0 or ordinal >= len(cols) or types[ordinal] != 245:
                                raise ValueError('JSON diff does not target a JSON column')
                            name = cols[ordinal]
                            after[name] = apply_json_ops(after.get(name), diff['ops'])
                    new = int(after['id'])
            if old is not None and (new is None or new != old):
                self.current['overlay'][obj, old] = None
            if new is not None:
                self.current['overlay'][obj, new] = after
            pk = int(before['id']) if after is None else int(after['id'])
            self.current['changes'].append({'object_id': obj, 'logical_table': logical_table, 'op': op_name(event_type), 'pk': pk, 'before': before, 'after': after})

    def _orders_backing_for_pk(self, pk):
        if not self.partitioned:
            return OBJ2
        if int(pk) < int(self.partition_boundary):
            return self.partition_backing['p_cold']
        return self.partition_backing['p_hot']

    def _apply_partitioned_orders_row(self, cols, types, event_type, rows):
        for rr in rows:
            bp = decode_image(types, rr['before_bitmap'], rr['before_data']) if 'before_data' in rr else None
            ap = decode_image(types, rr['after_bitmap'], rr['after_data']) if 'after_data' in rr else None

            if event_type == 'WRITE_ROWS':
                if ap is None or 0 not in ap:
                    raise ValueError('insert missing primary key')
                after = self.apply_partial(None, ap, cols)
                pk = int(after['id'])
                obj = self._orders_backing_for_pk(pk)
                self.current['overlay'][obj, pk] = after
                self.current['changes'].append({
                    'object_id': obj,
                    'logical_table': 'orders',
                    'op': 'insert',
                    'pk': pk,
                    'before': None,
                    'after': after,
                })
                continue

            if bp is None or 0 not in bp:
                raise ValueError('before image missing primary key')
            old_pk = int(bp[0])
            old_obj = self._orders_backing_for_pk(old_pk)
            base = self.read_tx_row(old_obj, old_pk)
            if base is None:
                raise ValueError(f'no visible base {old_obj}:{old_pk}')
            for ordinal, value in bp.items():
                name = cols[ordinal]
                if base.get(name) != value:
                    raise ValueError(f'before-image disagrees with reconstructed state for {old_obj}:{old_pk}:{name}')
            before = dict(base)

            if event_type == 'DELETE_ROWS':
                self.current['overlay'][old_obj, old_pk] = None
                self.current['changes'].append({
                    'object_id': old_obj,
                    'logical_table': 'orders',
                    'op': 'delete',
                    'pk': old_pk,
                    'before': before,
                    'after': None,
                })
                continue

            after = self.apply_partial(before, ap or {}, cols)
            if event_type == 'PARTIAL_UPDATE_ROWS':
                for diff in rr.get('json_diffs', []):
                    ordinal = int(diff['ordinal'])
                    if ordinal < 0 or ordinal >= len(cols) or types[ordinal] != 245:
                        raise ValueError('JSON diff does not target a JSON column')
                    name = cols[ordinal]
                    after[name] = apply_json_ops(after.get(name), diff['ops'])

            new_pk = int(after['id'])
            new_obj = self._orders_backing_for_pk(new_pk)
            if old_obj != new_obj or old_pk != new_pk:
                self.current['overlay'][old_obj, old_pk] = None
            self.current['overlay'][new_obj, new_pk] = after
            self.current['changes'].append({
                'object_id': new_obj,
                'logical_table': 'orders',
                'op': 'update',
                'pk': new_pk,
                'before': before,
                'after': after,
            })

    def row_event(self, e):
        m = self.table_maps.get(int(e['table_id']))
        if m is None:
            raise ValueError(f"row event without TABLE_MAP {e['table_id']}")
        table = m.get('table')
        if table == 'orders' and self.partitioned:
            self._apply_partitioned_orders_row(m['columns'], m['types'], e['event_type'], e['rows'])
            return
        if table == 'orders_stage' and self.partitioned:
            if self.stage_backing is None:
                raise ValueError('orders_stage has no active physical backing')
            self.apply_row(self.stage_backing, m['columns'], m['types'], e['event_type'], e['rows'], logical_table='orders_stage')
            return
        self.apply_row(m['object_id'], m['columns'], m['types'], e['event_type'], e['rows'], logical_table=table)

    def replay_override(self, rows):
        for r in rows:
            c = self.catalog.get(r['table'])
            if c is None:
                raise ValueError(f"copy row refers to unknown table {r['table']}")
            cols = list(c['columns'])
            types = list(c['types'])
            if types != list(r['types']):
                raise ValueError('replicated copy type vector disagrees with catalog')
            if r['table'] == 'orders' and self.partitioned:
                self._apply_partitioned_orders_row(cols, types, r['event_type'], r['rows'])
            elif r['table'] == 'orders_stage' and self.partitioned:
                if self.stage_backing is None:
                    raise ValueError('orders_stage has no active physical backing')
                self.apply_row(self.stage_backing, cols, types, r['event_type'], r['rows'], logical_table='orders_stage')
            else:
                self.apply_row(c['object_id'], cols, types, r['event_type'], r['rows'], logical_table=r['table'])

    def apply_ddl(self, t):
        if t['source_uuid'] == self.checkpoint_source:
            return
        for sql in t['ddl']:
            create_stage = re.match('CREATE\\s+TABLE\\s+orders_stage\\s+LIKE\\s+orders\\s*$', sql, re.I)
            if create_stage:
                if 'orders_stage' in self.catalog or self.stage_backing is not None:
                    raise ValueError('orders_stage already exists')
                src = self.catalog.get('orders')
                if src is None:
                    raise ValueError('CREATE TABLE orders_stage LIKE orders without orders catalog')
                self.stage_backing = self.storage_objects['orders_stage_object_id']
                self.physical.setdefault(self.stage_backing, {})
                self.catalog['orders_stage'] = {'object_id': self.stage_backing, 'columns': list(src['columns']), 'types': list(src['types'])}
                continue
            part = re.match('ALTER\\s+TABLE\\s+orders\\s+PARTITION\\s+BY\\s+RANGE\\s*\\(id\\)\\s*\\(PARTITION\\s+p_cold\\s+VALUES\\s+LESS\\s+THAN\\s*\\((\\d+)\\)\\s*,\\s*PARTITION\\s+p_hot\\s+VALUES\\s+LESS\\s+THAN\\s+MAXVALUE\\s*\\)\\s*$', sql, re.I)
            if part:
                if self.partitioned:
                    raise ValueError('orders is already partitioned')
                boundary = int(part.group(1))
                self.partitioned = True
                self.partition_boundary = boundary
                cold = self.storage_objects['p_cold_object_id']
                hot = self.storage_objects['p_hot_object_id']
                if cold != OBJ2:
                    raise ValueError('this capture expects the cold partition to retain the orders root object')
                self.partition_backing = {'p_cold': cold, 'p_hot': hot}
                self.physical.setdefault(hot, {})
                for pk in sorted([k for k in self.physical[OBJ2] if int(k) >= boundary]):
                    self.physical[hot][pk] = self.physical[OBJ2].pop(pk)
                continue
            exch = re.match('ALTER\\s+TABLE\\s+orders\\s+EXCHANGE\\s+PARTITION\\s+p_hot\\s+WITH\\s+TABLE\\s+orders_stage(?:\\s+WITHOUT\\s+VALIDATION)?\\s*$', sql, re.I)
            if exch:
                if not self.partitioned or self.stage_backing is None:
                    raise ValueError('EXCHANGE PARTITION requires partitioned orders and orders_stage')
                hot = self.partition_backing['p_hot']
                self.partition_backing['p_hot'], self.stage_backing = (self.stage_backing, hot)
                self.catalog['orders_stage']['object_id'] = self.stage_backing
                continue
            if re.match('RENAME\\s+TABLE\\s+orders\\s+TO\\s+orders_archive\\s*,\\s*_orders_new\\s+TO\\s+orders\\s*$', sql, re.I):
                old = self.catalog.pop('orders')
                shadow = self.catalog.pop('_orders_new')
                self.catalog['orders_archive'] = old
                self.catalog['orders'] = shadow
                continue
            add = re.search('ALTER\\s+TABLE\\s+(\\w+)\\s+ADD\\s+COLUMN\\s+(\\w+)\\s+([A-Z]+(?:\\(\\d+\\))?).*?\\s+AFTER\\s+(\\w+)\\s*$', sql, re.I)
            if add and add.group(1) in self.catalog:
                table, col, typ, after = add.groups()
                c = self.catalog[table]
                if col in c['columns']:
                    raise ValueError(f'column already exists: {col}')
                idx = c['columns'].index(after) + 1
                c['columns'].insert(idx, col)
                c['types'].insert(idx, sql_type_code(typ))
                obj = c['object_id']
                default = 0 if re.search('\\bDEFAULT\\s+0\\b', sql, re.I) else None
                for row in self.physical[obj].values():
                    row[col] = default
                continue
            drop = re.search('ALTER\\s+TABLE\\s+(\\w+)\\s+DROP\\s+COLUMN\\s+(\\w+)\\s*$', sql, re.I)
            if drop and drop.group(1) in self.catalog:
                table, col = drop.groups()
                c = self.catalog[table]
                idx = c['columns'].index(col)
                c['columns'].pop(idx)
                c['types'].pop(idx)
                obj = c['object_id']
                for row in self.physical[obj].values():
                    row.pop(col, None)
                continue
            mod = re.search('ALTER\\s+TABLE\\s+(\\w+)\\s+MODIFY\\s+COLUMN\\s+(\\w+)\\s+([A-Z]+(?:\\(\\d+\\))?).*?\\s+AFTER\\s+(\\w+)\\s*$', sql, re.I)
            if mod and mod.group(1) in self.catalog:
                table, col, typ, after = mod.groups()
                c = self.catalog[table]
                idx = c['columns'].index(col)
                c['columns'].pop(idx)
                c['types'].pop(idx)
                newidx = c['columns'].index(after) + 1
                c['columns'].insert(newidx, col)
                c['types'].insert(newidx, sql_type_code(typ))
                continue

    def commit(self, t, e, xa=False):
        self.commit_counter += 1
        t['commit_file'] = e['binlog_file']
        t['commit_pos'] = int(e['end_pos'])
        t['commit_coord'] = event_coord(e)
        t['commit_order'] = self.commit_counter
        t['xa_commit'] = xa
        for (obj, pk), after in t['overlay'].items():
            if after is None:
                self.physical[obj].pop(pk, None)
            else:
                self.physical[obj][pk] = dict(after)
        self.apply_ddl(t)
        self.committed.append(t)
        if t.get('cutover_query'):
            if self.cutover is not None:
                raise ValueError('more than one authoritative cutover')
            self.cutover = (t, e)

    def replay_source(self, source, events, allowed=None, stop=None, overrides=None):
        overrides = overrides or {}
        self.table_maps.clear()
        skipping = False
        for e in events:
            if stop is not None and event_coord(e) > stop:
                break
            et = e['event_type']
            if et == 'GTID':
                if allowed is not None and (not gtid_in_set(e['gtid'], allowed)):
                    break
                skipping = False
                self.start_tx(e['gtid'], source, overrides.get(e['gtid']))
            elif skipping:
                continue
            elif et == 'ROTATE':
                self.table_maps.clear()
            elif et == 'TABLE_MAP':
                self.bind_map(e, source)
            elif et == 'XA_START':
                if self.current is None:
                    raise ValueError('XA_START outside transaction')
                self.current['xa'] = True
                self.current['xid'] = e['xid']
            elif et in ('WRITE_ROWS', 'UPDATE_ROWS', 'PARTIAL_UPDATE_ROWS', 'DELETE_ROWS'):
                if self.current is None:
                    raise ValueError('row outside transaction')
                if self.current.get('override') is None:
                    self.row_event(e)
            elif et == 'QUERY' and self.current:
                sql = e.get('sql', '')
                self.current['ddl'].append(sql)
                if 'RENAME TABLE orders TO orders_archive' in sql:
                    self.current['cutover_query'] = True
            elif et == 'XA_PREPARE':
                if self.current is None:
                    raise ValueError('XA_PREPARE without transaction')
                if self.current.get('override') is not None:
                    self.replay_override(self.current['override'])
                self.prepared[e['xid']] = self.current
                self.current = None
            elif et == 'XA_ROLLBACK':
                self.prepared.pop(e['xid'], None)
            elif et == 'XA_COMMIT':
                t = self.prepared.pop(e['xid'])
                self.commit(t, e, True)
            elif et == 'XID':
                t = self.current
                self.current = None
                if t is None:
                    raise ValueError('XID without transaction')
                if t.get('override') is not None:
                    self.current = t
                    self.replay_override(t['override'])
                    self.current = None
                self.commit(t, e, False)

    def build_replica_overrides(self, predecessor, successor, allowed_predecessor, successor_native_start, predecessor_events):
        origin = tx_copies(predecessor_events)
        successor_copies = tx_copies(self.streams[successor][:successor_native_start])
        out = {}
        for g, a in origin.items():
            if gtid_sid(g) != predecessor or not gtid_in_set(g, allowed_predecessor) or g not in successor_copies:
                continue
            out[g] = scs(a, successor_copies[g])
        return out

    def build_passive_replica_overrides(self, final, final_events, chain):
        origin = tx_copies(final_events)
        passive = []
        chain_set = set(chain)
        for source, events in self.streams.items():
            if source in chain_set:
                continue
            copies = tx_copies(events)
            if copies:
                passive.append(copies)
        out = {}
        for gtid, seq in origin.items():
            if gtid_sid(gtid) != final:
                continue
            observed = [seq]
            observed.extend(c[gtid] for c in passive if gtid in c)
            if len(observed) < 2:
                continue
            union = {token(item) for copy_seq in observed for item in copy_seq}
            if len(union) <= len(seq):
                continue
            out[gtid] = merge_partial_orders(observed)
        return out

    def run_merge_handoff(self, info):
        base, side, final, final_start, final_frontier, fork_frontier = info
        chain = self.linear_chain_to(base, excluded={side, final})
        native_start = {source: self.source_native_info(source)[0] for source in chain[1:]}

        # Recover the ordinary lineage up to the branch point.
        for i, (predecessor, successor) in enumerate(zip(chain, chain[1:])):
            successor_start, frontier = self.source_native_info(successor)
            predecessor_start = 0 if predecessor == self.checkpoint_source else native_start[predecessor]
            predecessor_events = self.streams[predecessor][predecessor_start:]
            overrides = self.build_replica_overrides(predecessor, successor, frontier, successor_start, predecessor_events)
            self.replay_source(predecessor, predecessor_events, allowed=frontier, overrides=overrides)
            if self.current is not None or self.prepared:
                raise ValueError(f'{predecessor}: promotion has unresolved transaction state')
            if i == 0:
                self.init_catalog_from_checkpoint_source()

        # The side branch forked from base at its inherited frontier. Replay base only
        # through that frontier before applying the merged prefix recorded by final.
        side_start, _ = self.source_native_info(side)
        base_events = self.streams[base][native_start[base]:]
        base_overrides = self.build_replica_overrides(base, side, fork_frontier, side_start, base_events)
        self.replay_source(base, base_events, allowed=fork_frontier, overrides=base_overrides)
        if self.current is not None or self.prepared:
            raise ValueError(f'{base}: fork point has unresolved transaction state')

        prefix = self.streams[final][:final_start]
        final_copies = tx_copies(prefix)
        origin_copies = {base: tx_copies(self.streams[base]), side: tx_copies(self.streams[side])}
        origin_commits = {base: self.origin_commit_events(base), side: self.origin_commit_events(side)}

        order = []
        for e in prefix:
            if e.get('event_type') != 'GTID':
                continue
            sid = gtid_sid(e['gtid'])
            if sid in (base, side) and gtid_in_set(e['gtid'], final_frontier):
                order.append(e['gtid'])

        expected = set()
        for number in final_frontier.get(base, set()) - fork_frontier.get(base, set()):
            expected.add(f'{base}:{number}')
        for number in final_frontier.get(side, set()):
            expected.add(f'{side}:{number}')
        if set(order) != expected or len(order) != len(expected):
            raise ValueError('terminal merge prefix does not account for exactly the inherited divergent GTIDs')

        for gtid in order:
            sid = gtid_sid(gtid)
            origin = origin_copies[sid].get(gtid)
            commit_event = origin_commits[sid].get(gtid)
            if origin is None or commit_event is None:
                raise ValueError(f'missing origin evidence for merged transaction {gtid}')
            observed = [origin]
            if gtid in final_copies and final_copies[gtid]:
                observed.append(final_copies[gtid])
            rows = merge_partial_orders(observed) if len(observed) > 1 else origin
            self.start_tx(gtid, sid, rows)
            self.replay_override(rows)
            t = self.current
            self.current = None
            self.commit(t, commit_event, False)

        final_events = self.streams[final][final_start:]
        final_overrides = self.build_passive_replica_overrides(final, final_events, chain + [side, final])
        self.replay_source(final, final_events, stop=self.stop_coord, overrides=final_overrides)
        return self

    def run(self):
        merge = self.merge_handoff_info()
        if merge is not None:
            return self.run_merge_handoff(merge)

        chain = self.promotion_chain()
        native_start = {}
        for source in chain[1:]:
            native_start[source] = self.source_native_info(source)[0]
        for i, (predecessor, successor) in enumerate(zip(chain, chain[1:])):
            successor_start, frontier = self.source_native_info(successor)
            predecessor_start = 0 if predecessor == self.checkpoint_source else native_start[predecessor]
            predecessor_events = self.streams[predecessor][predecessor_start:]
            overrides = self.build_replica_overrides(predecessor, successor, frontier, successor_start, predecessor_events)
            self.replay_source(predecessor, predecessor_events, allowed=frontier, overrides=overrides)
            if self.current is not None or self.prepared:
                raise ValueError(f'{predecessor}: promotion has unresolved transaction state')
            if i == 0:
                self.init_catalog_from_checkpoint_source()
        final = chain[-1]
        final_events = self.streams[final][native_start[final]:]
        final_overrides = self.build_passive_replica_overrides(final, final_events, chain)
        self.replay_source(final, final_events, stop=self.stop_coord, overrides=final_overrides)
        return self

    def paired_pre_cutover(self, t):
        queues = defaultdict(deque)
        for c in t['changes']:
            if c['object_id'] == OBJ2:
                queues[c['op'], c['pk']].append(c)
        logical = []
        for c in t['changes']:
            if c['object_id'] != OBJ1:
                continue
            q = queues.get((c['op'], c['pk']))
            if q:
                logical.append(q.popleft())
        return logical

    def output(self):
        if not self.cutover:
            raise ValueError('authoritative cutover not recovered')
        cut_t, cut_e = self.cutover
        cut_order = cut_t['commit_order']
        txs = []
        changes = []
        for t in self.committed:
            if gtid_in_set(t['gtid'], self.delivered):
                continue
            if t['commit_order'] < cut_order:
                logical = self.paired_pre_cutover(t)
                mode = 'xa_dual_write' if t['xa_commit'] else 'dual_write'
                epoch = 'pre_cutover'
            elif t['gtid'] != cut_t['gtid']:
                logical = [c for c in t['changes'] if c.get('logical_table') == 'orders']
                mode = 'direct_v2'
                epoch = 'post_cutover'
            else:
                logical = []
                mode = 'direct_v2'
                epoch = 'post_cutover'
            if not logical:
                continue
            txs.append({'gtid': t['gtid'], 'commit_file': t['commit_file'], 'commit_pos': t['commit_pos'], 'mode': mode, 'change_count': len(logical)})
            for i, c in enumerate(logical, 1):

                def canon(r):
                    return None if r is None else {n: r.get(n) for n in self.v2_columns}
                changes.append({'gtid': t['gtid'], 'ordinal': i, 'op': c['op'], 'pk': c['pk'], 'before': canon(c['before']), 'after': canon(c['after']), 'source_object_id': c['object_id'], 'schema_epoch': epoch})
        return {'recovered_through': self.checkpoint['stop'], 'cutover': {'gtid': cut_t['gtid'], 'commit_file': cut_e['binlog_file'], 'commit_pos': int(cut_e['end_pos']), 'old_object_id': OBJ1, 'new_object_id': OBJ2}, 'transactions': txs, 'changes': changes}
