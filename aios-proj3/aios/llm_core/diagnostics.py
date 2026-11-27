"""Provider diagnostics shared by the connection check and model calls."""
import json
import socket
import ssl
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MESSAGES = {
    'unconfigured': '请先保存并选用模型配置。',
    'network': '无法连接模型服务，请核对服务地址和网络。',
    'dns': '无法解析模型服务地址，请检查 DNS。',
    'tls': '模型服务的 TLS 握手失败，请检查服务端网络或代理。',
    'refused': '服务或代理拒绝连接，请核对端口是否在监听。',
    'proxy_auth': '代理要求认证，请核对代理配置。',
    'request_timeout': '本次模型请求已超时，未自动重试。',
    'service_unavailable': '本地模型服务不可用，请核对服务状态。',
    'timeout': '连接模型服务超时，请检查网络和代理。',
    'auth': '模型服务拒绝认证，请核对当前配置的密钥。',
    'forbidden': '模型服务拒绝访问，请检查账户和模型权限。',
    'balance': '模型服务报告余额不足。',
    'rate_limit': '模型服务限流，请稍后再试。',
    'model': '模型标识不可用，请核对当前配置的模型名和访问权限。',
    'request': '模型服务拒绝了请求参数，请核对模型名和接口设置。',
    'upstream': '模型服务返回异常，请检查供应商状态。',
    'stream': '模型响应未完整结束，请检查供应商连接和输出预算。',
    'catalog': '已连接服务，但无法通过模型列表确认当前模型。',
    'available': '模型列表已返回，当前配置的模型可见。',
}


def transport_issue(error):
    if isinstance(error, HTTPError):
        return {400: 'request', 401: 'auth', 402: 'balance', 403: 'forbidden',
                404: 'request', 407: 'proxy_auth', 422: 'request', 429: 'rate_limit'}.get(error.code, 'upstream')
    reason = getattr(error, 'reason', error)
    if isinstance(reason, TimeoutError):
        return 'timeout'
    if isinstance(reason, socket.gaierror):
        return 'dns'
    if isinstance(reason, ssl.SSLError):
        return 'tls'
    if isinstance(reason, ConnectionRefusedError):
        return 'refused'
    return 'network'


def completion_issue(error):
    if isinstance(error, HTTPError) and error.code in (400, 404, 422):
        try:
            body = json.loads(error.read(16384))
            detail = body.get('error') if isinstance(body, dict) else None
            if isinstance(detail, dict) and (
                detail.get('code') in ('model_not_found', 'invalid_model')
                or detail.get('message') in ('Model Not Exist', 'Model Not Exist.')
            ):
                return 'model'
        except (ValueError, OSError, HTTPException, RecursionError):
            pass
    return transport_issue(error)


def check_model_list(config):
    request = Request(config['hostname'].rstrip('/') + '/models')
    request.add_unredirected_header('Authorization', 'Bearer ' + config['api_key'])
    try:
        with urlopen(request, timeout=6) as response:
            # Cap the provider response before decoding its model catalog.
            raw = response.read(1048577)
        if len(raw) > 1048576:
            return 'catalog'
        catalog = json.loads(raw)
    except HTTPError as exc:
        with exc:
            return 'catalog' if exc.code in (404, 405) else transport_issue(exc)
    except (URLError, OSError, HTTPException) as exc:
        return transport_issue(exc)
    except (ValueError, RecursionError):
        return 'catalog'
    if not isinstance(catalog, dict) or not isinstance(catalog.get('data'), list):
        return 'catalog'
    for row in catalog['data']:
        if isinstance(row, dict) and row.get('id') == config['name']:
            return 'available'
    return 'catalog' if catalog.get('has_more') else 'model'
