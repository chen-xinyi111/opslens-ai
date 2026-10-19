"""Private consistent backup and isolated restore verification; no workers or model calls."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from opslens import db
from opslens.config import docker_executable
from psycopg import sql

def fingerprints(conn):
    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
    tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    result = {}
    for item in tables:
        rows = conn.execute(sql.SQL('SELECT to_jsonb(t)::text AS value FROM {} t ORDER BY to_jsonb(t)::text').format(sql.Identifier(item['tablename']))).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(row['value'].encode('utf-8') + b'\n')
        result[item['tablename']] = {'count': len(rows), 'sha256': digest.hexdigest()}
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--check-active', action='store_true')
    args = parser.parse_args()
    with db.connect() as c:
        if c.execute("SELECT to_regclass('diagnostic_turns') AS t").fetchone()['t']:
            if c.execute("SELECT count(*) AS n FROM diagnostic_turns WHERE status IN ('queued','running')").fetchone()['n']:
                raise RuntimeError('Wait for active troubleshooting before taking a backup')
        for table in ['investigations', 'kb_tasks', 'collections']:
            if c.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE status IN ('queued','running')").format(sql.Identifier(table))).fetchone()['n']:
                raise RuntimeError('Wait for active tasks before taking a release backup')
        if c.execute("SELECT count(*) AS n FROM recovery_plans WHERE status IN ('approved','executing','verifying')").fetchone()['n']:
            raise RuntimeError('Wait for active recovery before taking a release backup')
    if args.check_active:
        print('No active tasks; backup window can begin')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = ROOT / '.archives' / stamp
    target.mkdir(parents=True, exist_ok=False)
    docker = docker_executable()
    ids = subprocess.check_output([docker, 'ps', '-q', '--filter', 'label=com.docker.compose.project=opslens-platform', '--filter', 'label=com.docker.compose.service=postgres'], text=True).split()
    if len(ids) != 1:
        raise RuntimeError('Expected exactly one running OpsLens database')
    container = ids[0]
    with db.connect() as c:
        baseline = fingerprints(c)
    with (target / 'platform.dump').open('wb') as output:
        subprocess.run([docker, 'exec', container, 'pg_dump', '-U', 'opslens', '-d', 'opslens', '-Fc', '--no-owner', '--no-privileges'], stdout=output, check=True)
    for folder in ['data/evaluation', 'docs']:
        shutil.copytree(ROOT / folder, target / folder)
    proof = target / 'proof'
    proof.mkdir()
    patterns = ['evaluation*.json', 'evaluation*.json.gz', 'demo*.json', 'durability*.json', 'durability*.png', 'delivery-*.json', 'delivery-*.png', 'product*.json', 'product*.png', 'product-necessary*.xml', 'product-necessary*.txt', 'product-necessary*.cjs', 'product-publication*.xml', 'product-publication*.txt']
    for pattern in patterns:
        for source in (ROOT / '.runtime').glob(pattern):
            if source.is_file():
                shutil.copy2(source, proof / source.name)
    for run in (ROOT / '.runtime').glob('value-validation-*'):
        if not run.is_dir() or not (run / 'results.json').is_file():continue
        destination=proof/run.name;destination.mkdir()
        for source in run.iterdir():
            if source.is_file() and not source.is_symlink() and source.name.endswith(('.json','.json.gz','.md','.py')):
                shutil.copy2(source,destination/source.name)
    revision = subprocess.check_output(['git', '-c', f'safe.directory={ROOT.as_posix()}', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    with (target / 'source.zip').open('wb') as output:
        subprocess.run(['git', '-c', f'safe.directory={ROOT.as_posix()}', 'archive', '--format=zip', revision], cwd=ROOT, stdout=output, check=True)
    previous = sorted((ROOT / '.archives').glob('*/manifest.json'))
    if previous:
        prior_revision = json.loads(previous[-1].read_text(encoding='utf-8'))['revision']
        if not re.fullmatch(r'[a-f0-9]{40,64}', prior_revision):raise ValueError('Invalid archived Git revision')
        with (target / 'source-previous.zip').open('wb') as output:
            subprocess.run(['git', '-c', f'safe.directory={ROOT.as_posix()}', 'archive', '--format=zip', prior_revision], cwd=ROOT, stdout=output, check=True)
    manifest = {'created_at': stamp, 'revision': revision, 'tables': baseline, 'restore_verified': False, 'files': {}}
    for file in sorted(target.rglob('*')):
        if file.is_file():
            manifest['files'][file.relative_to(target).as_posix()] = {'bytes': file.stat().st_size, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
    manifest_path = target / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.verify:
        restore_db = 'opslens_restore_' + stamp.lower()
        subprocess.run([docker, 'exec', container, 'createdb', '-U', 'opslens', restore_db], check=True)
        with (target / 'platform.dump').open('rb') as source:
            subprocess.run([docker, 'exec', '-i', container, 'pg_restore', '-U', 'opslens', '-d', restore_db, '--exit-on-error', '--no-owner', '--no-privileges'], stdin=source, check=True)
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        from opslens.config import platform_dsn
        params = conninfo_to_dict(platform_dsn())
        params['dbname'] = restore_db
        with psycopg.connect(**params, row_factory=dict_row) as c:
            restored = fingerprints(c)
        if restored != baseline:
            raise RuntimeError('Restore table fingerprints differ from baseline')
        manifest.update(restore_verified=True, verification_database=restore_db)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (target / 'RESTORE.md').write_text('私有归档，请勿上传。platform.dump 为 PostgreSQL 16/pgvector 自定义备份。\n'
        '先校验 manifest.json 文件摘要，再在独立空数据库执行 pg_restore --exit-on-error --no-owner --no-privileges。\n'
        '不要覆盖原数据库；验证时不启动 OpsLens 任务处理器。还原不包含密钥，需另行配置。\n'
        'source.zip保存归档版本的受跟踪源码；source-previous.zip（如有）保留前次归档对应实现。\n'
        '表级数量与内容摘要位于 manifest.json；验证数据库独立保留，不含运行中的任务处理器。\n', encoding='utf-8')
    guide=target/'RESTORE.md'
    manifest['files']['RESTORE.md']={'bytes':guide.stat().st_size,'sha256':hashlib.sha256(guide.read_bytes()).hexdigest()}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'archive': str(target), 'restore_verified': manifest['restore_verified'], 'tables': len(baseline)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
