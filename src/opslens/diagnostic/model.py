"""Explicit profiles and a database-reserved daily request budget. No implicit fallback."""
import os
import re
from uuid import uuid4
from urllib.parse import urlsplit
import httpx
from opslens import db
from opslens.rag.store import jsonb
from opslens.rag.provider import ModelBlocked,ModelFailure,QuotaGuard,api_base
from opslens.investigation.model import GenerationProvider
from .contracts import ModelSettings
from .intake import clean

DAY="(date_trunc('day',now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai')"

def validate_settings(value):
    url=urlsplit(value.base_url)
    if url.username or url.password or url.query or url.fragment or not url.hostname:raise ValueError('接口地址不能包含凭据、查询参数或片段')
    if value.mode=='free':
        if value.key_env!='DASHSCOPE_APIKEY' or value.base_url.rstrip('/')!=api_base():raise ValueError('免费模式使用已核验的百炼北京端点和DASHSCOPE_APIKEY')
        if value.model not in ('qwen3.8-max','deepseek-v4.1-flash'):raise ValueError('免费模式模型不在既有范围内')
    elif url.scheme!='https' and not(url.scheme=='http' and url.hostname in ('localhost','127.0.0.1','::1')):
        raise ValueError('兼容接口需要HTTPS；仅本机模型允许HTTP')
    if value.mode=='compatible' and not url.path.rstrip('/').endswith('/v1'):raise ValueError('兼容接口填写以/v1结尾的基础地址')
    if any(not re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z]{2,}',h) for h in value.official_hosts):raise ValueError('官方域名必须是小写完整域名，不含路径或通配符')
    return value

def config(c):
    row=c.execute('SELECT * FROM diagnostic_configs ORDER BY version DESC LIMIT 1').fetchone()
    if not row:raise ValueError('模型设置尚未初始化')
    return row

def used(c):return c.execute('SELECT count(*) AS n FROM diagnostic_requests WHERE created_at>='+DAY).fetchone()['n']

def key(cfg):return os.environ.get(cfg['settings']['key_env'],'')

def dynamic_secrets(connection=None):
    from contextlib import nullcontext
    try:
        with (db.connect() if connection is None else nullcontext(connection)) as c:
            return [os.environ.get(r['settings']['key_env'],'') for r in c.execute('SELECT settings FROM diagnostic_configs').fetchall()]
    except Exception:raise ModelBlocked('无法读取脱敏密钥配置，已停止处理；请恢复数据库后重试') from None

def guard_messages(messages,secrets):
    import json
    body=json.dumps(messages,ensure_ascii=False)
    # Check both raw and JSON-escaped forms, including user-content JSON nested in messages.
    variants=[variant for secret in secrets if secret for variant in (secret,json.dumps(secret,ensure_ascii=False)[1:-1])]
    if any(variant in body or any(variant in message.get('content','') for message in messages) for variant in variants):
        raise ModelBlocked('发送正文包含已知密钥，已阻止外发；请重新整理脱敏资料')

def readiness(cfg):
    value=validate_settings(ModelSettings(**cfg['settings']))
    if not value.enabled:raise ModelBlocked('新排查模型尚未启用；仍可整理和查看本机资料')
    if not key(cfg):raise ModelBlocked('未配置服务端环境变量 '+value.key_env)
    if value.mode=='free':QuotaGuard(key=key(cfg),endpoint=value.base_url).check(value.model)
    with db.connect() as c:
        if used(c)>=value.daily_limit:raise ModelBlocked('今日模型请求预算已用完')
    return value

def settings_status():
    with db.connect() as c:row=config(c);count=used(c)
    try:readiness(row);available=True;reason='模型配置可用；开始分析后才发送选中资料'
    except (ValueError,ModelBlocked) as exc:available=False;reason=str(exc)
    return row|{'available':available,'reason':reason,'used_today':count}

def save_settings(data):
    validate_settings(data)
    with db.connect() as c:
        c.execute('SELECT pg_advisory_xact_lock(5010)')
        old=config(c)
        if old['settings']==data.model_dump():return old
        return c.execute('INSERT INTO diagnostic_configs(id,version,settings) VALUES (%s,%s,%s) RETURNING *',(uuid4(),old['version']+1,jsonb(data.model_dump()))).fetchone()

def reserve(turn_id,stage,cfg):
    readiness(cfg)
    with db.connect() as c:
        c.execute('SELECT pg_advisory_xact_lock(5011)')
        if str(config(c)['id'])!=str(cfg['id']):raise ModelBlocked('模型设置已变化，请重新预览并确认')
        if used(c)>=cfg['settings']['daily_limit']:raise ModelBlocked('今日模型请求预算已用完')
        existing=c.execute('SELECT * FROM diagnostic_requests WHERE turn_id=%s AND stage=%s',(turn_id,stage)).fetchone()
        if existing:raise ModelBlocked('该请求已有记录，禁止重复发送')
        rid=uuid4()
        c.execute('INSERT INTO diagnostic_requests(id,turn_id,stage) VALUES (%s,%s,%s)',(rid,turn_id,stage))
        return rid

class FreeProvider(GenerationProvider):
    def __init__(self,cfg):super().__init__(model=cfg['settings']['model']);self.cap=min(1800,cfg['settings']['max_output'])
    def generate(self,messages,tools):
        payload={'model':self.name,'messages':messages,'temperature':.1,'max_tokens':self.cap,'enable_thinking':False,'enable_search':False}
        value=self._call(self.name,'/services/aigc/text-generation/generation',payload,len(str(payload).encode())+self.cap)
        choice=value['output']['choices'][0]
        return {'content':choice['message'].get('content',''),'usage':value.get('usage'), 'finish_reason':choice.get('finish_reason')}

class Provider:
    def generate(self,turn_id,stage,cfg,messages):
        with db.connect() as c:secrets=dynamic_secrets(c)
        guard_messages(messages,[*secrets,key(cfg)])
        rid=reserve(turn_id,stage,cfg)
        try:
            if cfg['settings']['mode']=='free':result=FreeProvider(cfg).generate(messages,[])
            else:
                with httpx.Client(timeout=httpx.Timeout(90,connect=10),trust_env=False,follow_redirects=False) as client:
                    response=client.post(cfg['settings']['base_url'].rstrip('/')+'/chat/completions',
                        headers={'Authorization':'Bearer '+key(cfg)},json={'model':cfg['settings']['model'],'messages':messages,'max_tokens':cfg['settings']['max_output'],'temperature':.1})
                    if response.status_code!=200:raise ModelFailure('模型请求失败，HTTP '+str(response.status_code)+'；不会自动重试')
                    data=response.json();choice=data['choices'][0]
                    result={'content':choice['message'].get('content',''),'usage':data.get('usage'),'finish_reason':choice.get('finish_reason')}
            usage=result.get('usage')
            known=isinstance(usage,dict) and isinstance(usage.get('total_tokens'),int) and usage['total_tokens']>=0
            with db.connect() as c:c.execute("UPDATE diagnostic_requests SET status=%s,usage=%s,finished_at=now() WHERE id=%s",('completed' if known else 'usage_unknown',jsonb(usage or {}),rid))
            return result|{'usage_unknown':not known}
        except Exception as exc:
            with db.connect() as c:c.execute("UPDATE diagnostic_requests SET status='unknown',error=%s,finished_at=now() WHERE id=%s",(clean(str(exc),[key(cfg)])[:500],rid))
            if isinstance(exc,(ModelBlocked,ModelFailure)):raise
            raise ModelFailure('模型响应未知或连接失败，记录已保留，不自动重试') from None
