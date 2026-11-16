"""Factual Runtime capability answers, separate from model training-time claims."""
import re

NETWORK_GUIDANCE=('Runtime已接入web.fetch，可读取公开HTTPS网页文本并记录来源和HTTP状态。'
 '需要当前网页信息时应调用该工具，不能因模型自身没有浏览器就声称整个Runtime无法联网。'
 '目前没有通用登录/复杂JavaScript浏览器或搜索引擎全网检索插件；不得冒充已执行这些操作。')


def network_question(prompt):
    if not isinstance(prompt,str) or len(prompt)>80:return False
    if re.search(r'https?://|www\.|帮我|替我|给我|查一下|查询|检索|搜索|读取|打开|今天|新闻|天气|价格|下载',prompt,re.I):return False
    return bool(re.search(r'你|runtime|agent',prompt,re.I) and re.search(r'能|可以|是否|支持|有没有|具备|有.*能力',prompt)
                and re.search(r'联网|上网|浏览网页|浏览能力|访问网页|网络访问',prompt))


def network_answer():
    from .computer import available
    if available():
        return ('可以。Runtime有公开HTTPS读取工具 `web.fetch`，也有NUC专用实验电脑工具 `computer.run`，能打开网页、观察页面、截图、点击、输入和滚动。\n\n'
                '浏览器位于独立虚拟桌面，没有你的个人桌面和账户凭据；公网访问经过受限GET桥接。账户登录、验证码绕过、支付或任意原生桌面软件控制尚未接入。\n\n'
                '这条回复只核对了已注册能力，没有声称刚刚浏览过网页。')
    return ('可以，Runtime 已接入网页读取工具 `web.fetch`。你可以给我一个公开的 HTTPS 链接，我可以实际读取网页文本、分析内容，并保留来源。\n\n'
            '当前支持的是网页文本读取；登录页面、复杂 JavaScript 交互和搜索引擎全网检索尚未接入。\n\n'
            '这条回复只核对了已注册能力，没有声称刚刚浏览过网页。')

def network_guidance():
    from .computer import available
    return (NETWORK_GUIDANCE if not available() else
            'Runtime已接入web.fetch和NUC专用实验浏览器computer.run，可实际读取公网HTTPS网页，必要时打开网页截图、点击、输入、滚动。不能因为模型本身没有浏览器就声称整个Runtime无法联网。浏览器不持有用户凭据，登录、支付、验证码绕过和个人桌面控制不在权限内；页面拒绝时报告实际状态。')
