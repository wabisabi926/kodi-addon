import datetime
import json
import locale
import platform
import queue
import random
import threading
import time
from typing import Optional, Dict, Any, List, Tuple

import xbmc
import xbmcaddon

ADDON = xbmcaddon.Addon()
_ADDON_ID = ADDON.getAddonInfo('id')
APTABASE_HOST = 'https://analytics.theintrodb.org'
APTABASE_APP_KEY = 'A-SH-5507621118'


def _fresh_setting(key: str) -> str:
    try:
        return xbmcaddon.Addon(_ADDON_ID).getSetting(key)
    except Exception:
        return ADDON.getSetting(key)


def _fresh_bool(key: str) -> bool:
    return _fresh_setting(key) == 'true'


def _utc_iso() -> str:
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def _new_session_id() -> str:
    epoch_seconds = int(time.time())
    return '{}{:08d}'.format(epoch_seconds, random.randint(0, 99999999))


def _get_config() -> Tuple[bool, str, str]:
    enabled = _fresh_bool('anonymous_usage_reporting')
    host = (APTABASE_HOST or '').strip()
    app_key = (APTABASE_APP_KEY or '').strip()
    host = host[:-1] if host.endswith('/') else host
    return enabled, host, app_key


class AptabaseReporter:
    def __init__(self) -> None:
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self._session_id = _new_session_id()
        self._session_started_at = time.time()
        self._thread = threading.Thread(target=self._worker, name='tidb-aptabase', daemon=True)
        self._thread.start()
        if _fresh_bool('debug_logging'):
            enabled, host, app_key = _get_config()
            safe_key = app_key[-6:] if app_key else ''
            xbmc.log('[TheIntroDB] Aptabase init enabled={} host={} key=*{}'.format(enabled, host, safe_key), xbmc.LOGINFO)

    def track(self, event_name: str, props: Optional[Dict[str, Any]] = None) -> None:
        enabled, host, app_key = _get_config()
        if not enabled or not host or not app_key or not event_name:
            return
        clean_props: Dict[str, Any] = {}
        if isinstance(props, dict):
            for k, v in props.items():
                if isinstance(v, (str, int, float)) or v is None:
                    clean_props[str(k)] = v
                else:
                    clean_props[str(k)] = str(v)
        try:
            self._q.put_nowait({
                'eventName': event_name,
                'props': clean_props,
            })
        except Exception:
            return

    def flush(self, timeout: float = 2.0) -> None:
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            if self._q.empty():
                return
            time.sleep(0.05)

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        try:
            self._thread.join(timeout=max(0.0, float(timeout)))
        except Exception:
            pass

    def _system_props(self) -> Dict[str, Any]:
        try:
            loc = locale.getdefaultlocale()[0] or ''
        except Exception:
            loc = ''
        try:
            app_version = ADDON.getAddonInfo('version') or ''
        except Exception:
            app_version = ''
        is_debug = _fresh_bool('debug_logging') if _fresh_setting('debug_logging') else False
        return {
            'locale': loc,
            'osName': platform.system(),
            'osVersion': platform.release(),
            'deviceModel': platform.machine(),
            'isDebug': bool(is_debug),
            'appVersion': app_version,
            'sdkVersion': 'kodi-addon@1',
        }

    def _ensure_session(self) -> None:
        if time.time() - self._session_started_at > 3600:
            self._session_id = _new_session_id()
            self._session_started_at = time.time()

    def _worker(self) -> None:
        buf: List[Dict[str, Any]] = []
        last_flush = time.time()
        flush_interval = 5.0
        max_batch = 25

        while not self._stop.is_set():
            got_item = None
            try:
                got_item = self._q.get(timeout=0.25)
                buf.append(got_item)
            except queue.Empty:
                pass
            except Exception:
                continue

            now = time.time()
            should_flush = (len(buf) >= max_batch) or (buf and (now - last_flush) >= flush_interval)
            if got_item and got_item.get('eventName') == 'service_started':
                should_flush = True
            if not should_flush:
                continue

            self._send_batch(buf[:max_batch])
            del buf[:max_batch]
            last_flush = now

        if buf:
            self._send_batch(buf)

    def _send_batch(self, items: List[Dict[str, Any]]) -> None:
        enabled, host, app_key = _get_config()
        if not enabled or not host or not app_key or not items:
            return

        self._ensure_session()
        payload = []
        sys_props = self._system_props()
        for item in items:
            payload.append({
                'timestamp': _utc_iso(),
                'sessionId': self._session_id,
                'eventName': item.get('eventName'),
                'systemProps': sys_props,
                'props': item.get('props') or {},
            })

        try:
            from urllib.request import Request, urlopen
            from urllib.error import HTTPError
        except ImportError:
            from urllib2 import Request, urlopen

        try:
            body = json.dumps(payload).encode('utf-8')
            req = Request('{}/api/v0/events'.format(host), data=body, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('App-Key', app_key)
            resp = urlopen(req, timeout=5)
            try:
                resp.read()
            except Exception:
                pass
            if _fresh_bool('debug_logging'):
                xbmc.log('[TheIntroDB] Aptabase sent {} event(s)'.format(len(payload)), xbmc.LOGINFO)
        except HTTPError as e:
            if _fresh_bool('debug_logging'):
                try:
                    body_text = e.read().decode('utf-8', 'replace')
                except Exception:
                    body_text = ''
                xbmc.log('[TheIntroDB] Aptabase HTTP {} {}'.format(e.code, body_text[:400]), xbmc.LOGWARNING)
        except Exception as e:
            if _fresh_bool('debug_logging'):
                xbmc.log('[TheIntroDB] Aptabase send failed: {}'.format(e), xbmc.LOGWARNING)
