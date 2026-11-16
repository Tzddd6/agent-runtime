"""Actual server clock with explicit IANA zone and provenance."""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError


def now(zone='Asia/Shanghai'):
    if not isinstance(zone,str) or not 1<=len(zone)<=64:raise ValueError('时区无效')
    try:tz=ZoneInfo(zone)
    except (ZoneInfoNotFoundError,ValueError):raise ValueError('未知IANA时区') from None
    value=datetime.now(tz)
    return {'iso8601':value.isoformat(timespec='seconds'),'timezone':zone,'unix_seconds':value.timestamp(),
            'source':'runtime_server_system_clock','display':value.strftime('%Y年%m月%d日 %H:%M:%S')+'（'+zone+'，Runtime服务器时间）','executed':True}
