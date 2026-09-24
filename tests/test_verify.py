from __future__ import annotations
import json
from pathlib import Path
ARTIFACT=Path('/app/output/recovered_cdc.json'); EXPECTED=Path(__file__).resolve().parent/'expected.json'
TOP={'recovered_through','cutover','transactions','changes'}
CUT={'gtid','commit_file','commit_pos','old_object_id','new_object_id'}
TX={'gtid','commit_file','commit_pos','mode','change_count'}
CH={'gtid','ordinal','op','pk','before','after','source_object_id','schema_epoch'}
ROW={'id','customer_id','amount_cents','status','note','tax_cents'}

def load(p): return json.loads(p.read_text())

def test_artifact_exists_and_is_json(): assert ARTIFACT.is_file(); load(ARTIFACT)
def test_schema_is_exact():
    d=load(ARTIFACT); assert set(d)==TOP; assert set(d['recovered_through'])=={'file','pos'}; assert set(d['cutover'])==CUT
    assert isinstance(d['transactions'],list) and isinstance(d['changes'],list)
    assert all(set(x)==TX for x in d['transactions']); assert all(set(x)==CH for x in d['changes'])
    assert all(x['mode'] in {'xa_dual_write','dual_write','direct_v2'} for x in d['transactions'])
    assert all(x['op'] in {'insert','update','delete'} for x in d['changes'])
    assert all(x['schema_epoch'] in {'pre_cutover','post_cutover'} for x in d['changes'])
    for x in d['changes']:
        for r in (x['before'],x['after']):
            if r is not None: assert set(r)==ROW

def test_recovered_boundaries_and_transactions():
    a=load(ARTIFACT); e=load(EXPECTED); assert a['recovered_through']==e['recovered_through']; assert a['cutover']==e['cutover']; assert a['transactions']==e['transactions']
def test_changefeed_matches(): assert load(ARTIFACT)['changes']==load(EXPECTED)['changes']
