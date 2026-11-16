"""Allowlisted public diagnostics. Never expose arbitrary exceptions or stderr."""
MESSAGES={
    'COMPUTER_SESSION_FAILED':'专用电脑会话没有完成，请检查执行端、页面或资源情况；没有自动重试模型请求。',
    'COMPUTER_STEP_LIMIT':'电脑操作已达到限步或时间上限；保留实际操作与截图，尚未确认任务完成。',
    'PLAN_INVALID':'协作计划未通过格式或工具权限校验，未执行其中的操作。',
    'TOOL_INVALID_ARGUMENTS':'工具参数无效；错误已返回给控制流程，需要修正参数后再验证。',
    'TOOL_EXECUTION_REJECTED':'工具未完成，需核对参数、权限或运行结果；没有确认任务交付。',
    'MODEL_UPSTREAM_FAILED':'模型服务调用未完成（本地网关 HTTP 502）。底层原因未保留，无法确定是供应商异常还是超时；远端计费状态未知，未自动重试。',
    'MODEL_TIMEOUT':'模型调用等待超时；远端计算和计费状态未知，未自动重试。',
    'MODEL_BUDGET_EXHAUSTED':'模型累计预算不足或调用槽位等待超时；请求未自动重试。',
    'MODEL_TRANSPORT_FAILED':'Worker 与本地模型网关通信失败，未自动重试。',
    'MODEL_OUTPUT_INCOMPLETE':'模型输出达到长度上限，结果不完整；没有把残缺内容作为成功交付。请分阶段生成或保存分块产物。',
    'TASK_TIMEOUT':'执行超过本地任务期限，已停止并回收资源。',
    'TASK_OOM':'任务超过内存限制，已停止；没有按相同预算盲目重试。',
    'TASK_FAILED':'本次执行未完成，请查看执行记录；没有自动重复提交模型请求。'
}


def failure(code):
    code=code if code in MESSAGES else 'TASK_FAILED'
    return {'code':code,'message':MESSAGES[code]}


def model_code(error):
    # Error text is inspected, never returned. Arbitrary clients may include secrets.
    message=str(error).lower()
    if any(s in message for s in ('预算','budget','槽位')):return 'MODEL_BUDGET_EXHAUSTED'
    if '连接失败或超时' in message:return 'MODEL_UPSTREAM_FAILED'
    if any(s in message for s in ('deadline','timeout','超时','期限')):return 'MODEL_TIMEOUT'
    return 'MODEL_UPSTREAM_FAILED'


def event_failure(event):
    value=event.get('result',{}).get('result',{})
    if isinstance(value,dict) and value.get('error_code')=='PLAN_INVALID':return failure('PLAN_INVALID')
    if isinstance(value,dict) and value.get('plugin') and value.get('ok') is False and value.get('error_code') in MESSAGES:
        return failure(value['error_code'])
    if event.get('state') not in ('failed','timeout','cancelled'):return None
    structured=event.get('result',{}).get('failure',{})
    if isinstance(structured,dict) and structured.get('code') in MESSAGES:return failure(structured['code'])
    if event.get('state')=='timeout':return failure('TASK_TIMEOUT')
    if str(event.get('error','')).startswith('OOM:'):return failure('TASK_OOM')
    # Recover the known old Worker failure without displaying its traceback.
    if 'HTTP Error 502' in str(event.get('output','')):return failure('MODEL_UPSTREAM_FAILED')
    if 'HTTP Error 429' in str(event.get('output','')):return failure('MODEL_BUDGET_EXHAUSTED')
    if 'SyntaxError:' in str(event.get('output','')):return failure('TOOL_INVALID_ARGUMENTS')
    return failure('TASK_FAILED')


def job_failure(job):
    for event in job.get('events',[]):
        value=event_failure(event)
        if value:return value
    return failure('TASK_FAILED')
