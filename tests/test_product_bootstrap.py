"""Empty-database startup with no key/index/runtime state or sibling project."""
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
import shutil
import psycopg
from psycopg.rows import dict_row
from fastapi.testclient import TestClient
from opslens import db,external
from opslens.api import app,lifespan
from opslens.config import platform_dsn,ROOT

def test_empty_database_bootstrap_needs_only_packaged_sources(monkeypatch,tmp_path):
    schema='product_clean_'+uuid4().hex
    with db.connect() as c:c.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    def connect():return psycopg.connect(platform_dsn(),row_factory=dict_row,options=f'-c search_path={schema},public')
    monkeypatch.setattr(db,'connect',connect)
    for source in ['src/opslens/external_schema.sql','data/external/qinghe']:
        dest=tmp_path/source;dest.parent.mkdir(parents=True,exist_ok=True)
        if (ROOT/source).is_dir():shutil.copytree(ROOT/source,dest)
        else:shutil.copy2(ROOT/source,dest)
    monkeypatch.setattr(external,'ROOT',tmp_path)
    monkeypatch.delenv('DASHSCOPE_APIKEY',raising=False)
    # TestClient performs the real application startup in this empty schema.
    try:
        with TestClient(app) as c:
            assert c.get('/api/v1/health').status_code==200
            assert c.get('/api/v1/health').json()['version']=='0.5.0'
            targets=c.get('/api/v1/external-targets').json()
            assert len(targets)==1 and targets[0]['compose_project']=='qinghe-after-sales'
            with connect() as conn:
                assert conn.execute('SELECT count(*) AS n FROM external_documents').fetchone()['n']==2
                assert conn.execute('SELECT count(*) AS n FROM kb_indexes').fetchone()['n']==0
                assert conn.execute('SELECT count(*) AS n FROM kb_model_calls').fetchone()['n']==0
            assert c.get('/api/v1/investigations/status').json()['available'] is False
            status=c.get('/api/v1/diagnostic/settings').json()
            assert status['settings']['enabled'] is False and status['available'] is False
            assert status['used_today']==0 and c.get('/api/v1/diagnostic/sessions').json()==[]
    finally:
        with connect() as conn:conn.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))
