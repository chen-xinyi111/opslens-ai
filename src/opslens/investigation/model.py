import json
from concurrent.futures import ThreadPoolExecutor
from opslens.rag.provider import DashScopeProvider,ModelFailure,model_http_client
MODEL="qwen3.8-max"
class GenerationProvider(DashScopeProvider):
    def __init__(self,*args,model=MODEL,**kwargs):
        if model not in (MODEL,'deepseek-v4.1-flash'):raise ValueError('生成模型不在已授权范围')
        super().__init__(*args,**kwargs);self.name=model
    def prepare_parallel(self):
        # Check quota first; establish two verified connections without credentials or data.
        self.guard.check(self.name)
        client=model_http_client()
        def connect(_):
            try:
                response=client.get(self._url(''),timeout=10)
                if response.status_code>=500:raise ModelFailure("模型服务连接检查失败，未发送调查资料")
                return response.status_code
            except ModelFailure:raise
            except Exception:raise ModelFailure("模型服务连接检查失败，未发送调查资料") from None
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(connect,range(2)))
    def _url(self,path):
        return self.base.removesuffix("/api/v1")+"/compatible-mode/v1/chat/completions"
    def _normalize(self,data):
        if not isinstance(data,dict):return data
        return {**data,"output":{"choices":data.get("choices",[])},"request_id":data.get("id")}
    def generate(self,messages,tools):
        payload={"model":self.name,"messages":messages,"temperature":0.1,"max_tokens":1800,"enable_thinking":False,"preserve_thinking":False,"enable_search":False,"tools":tools}
        if self.name=='deepseek-v4.1-flash':payload.pop('preserve_thinking')
        size=len(json.dumps(payload,ensure_ascii=False).encode())
        if size>90000:raise ModelFailure("调查上下文超出保守长度上限")
        data=self._call(self.name,"/services/aigc/text-generation/generation",payload,size+1800)
        try:
            choice=data["output"]["choices"][0]
            if choice.get("finish_reason")=="length":raise ValueError()
            message=choice["message"]
            if message.get("role")!="assistant" or not isinstance(message.get("content",""),str):raise ValueError()
            calls=message.get("tool_calls",[])
            if not isinstance(calls,list) or len(calls)>12:raise ValueError()
            return {**message,"provider_request_id":data.get("request_id"),"usage":data.get("usage",{})}
        except (ValueError,KeyError,IndexError,TypeError):
            raise ModelFailure("调查模型响应格式不合法或输出截断") from None
