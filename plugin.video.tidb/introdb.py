# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 TheIntroDB
#
import threading
import json
import time
import xbmc
import xbmcaddon
from typing import Optional, Dict, Any, Tuple, List, Union

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import Request, urlopen, HTTPError, URLError

ADDON = xbmcaddon.Addon()
_ADDON_ID = ADDON.getAddonInfo('id')

API_BASE = 'https://api.theintrodb.org/v3'
MIN_REQUEST_GAP = 0.4  # small gap between requests
_last_request_time = 0.0
_rate_limit_until = 0.0
_rate_limit_lock = threading.Lock()


def _debug_logging() -> bool:
    return ADDON.getSetting('debug_logging') == 'true'


def _log_resp(body: str) -> None:
    if not _debug_logging():
        return
    snippet = body[:500] if len(body) > 500 else body
    xbmc.log('[TheIntroDB] TheIntroDB response: {}'.format(snippet), xbmc.LOGINFO)


def _get_api_key() -> str:
    return (ADDON.getSetting('introdb_api_key') or '').strip()


def _is_enabled() -> bool:
    try:
        return xbmcaddon.Addon(_ADDON_ID).getSetting('introdb_enabled') == 'true'
    except Exception:
        return ADDON.getSetting('introdb_enabled') == 'true'


def _wait_rate_limit() -> bool:
    global _last_request_time
    with _rate_limit_lock:
        now = time.time()
        if now < _rate_limit_until:
            xbmc.log('[TheIntroDB] TheIntroDB rate-limited until {:.0f}'.format(
                _rate_limit_until), xbmc.LOGINFO)
            return False
        gap = now - _last_request_time
        if gap < MIN_REQUEST_GAP:
            time.sleep(MIN_REQUEST_GAP - gap)
        _last_request_time = time.time()
    return True


def _do_request(url: str, api_key: str) -> Optional[Dict[str, Any]]:
    global _rate_limit_until
    req = Request(url)
    req.add_header('Accept', 'application/json')
    req.add_header('User-Agent', 'TheIntroDB Kodi Addon/1.0')
    if api_key:
        req.add_header('Authorization', 'Bearer {}'.format(api_key))

    try:
        resp = urlopen(req, timeout=8)
        body = resp.read().decode('utf-8')
        data = json.loads(body)
        _log_resp(body)
        return data
    except HTTPError as e:
        if e.code == 429:
            retry = 300
            for header in ('X-UsageLimit-Reset', 'X-RateLimit-Reset', 'Retry-After'):
                val = e.headers.get(header)
                if val:
                    try:
                        retry = int(val)
                    except ValueError:
                        pass
                    break
            with _rate_limit_lock:
                _rate_limit_until = time.time() + retry
            xbmc.log('[TheIntroDB] TheIntroDB 429 rate limited for {}s'.format(retry),
                     xbmc.LOGWARNING)
        elif e.code == 404:
            xbmc.log('[TheIntroDB] TheIntroDB 404: not in database', xbmc.LOGINFO)
        else:
            xbmc.log('[TheIntroDB] TheIntroDB HTTP {}'.format(e.code), xbmc.LOGWARNING)
        return None
    except URLError as e:
        xbmc.log('[TheIntroDB] TheIntroDB network error: {}'.format(e.reason),
                 xbmc.LOGWARNING)
        return None
    except Exception as e:
        xbmc.log('[TheIntroDB] TheIntroDB request failed: {}'.format(e),
                 xbmc.LOGERROR)
        return None


def _pick_best_segment(segments: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    # intro array may have multiple rows — take best score
    if not segments:
        return None, None

    best = None
    best_score = -1.0
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        start = seg.get('start_ms')
        end = seg.get('end_ms')
        if start is None:
            start = 0
        if end is None:
            continue
        if end <= start:
            continue
        conf = seg.get('confidence') if seg.get('confidence') is not None else 0.5
        count = seg.get('submission_count', 1)
        score = float(conf) + count * 0.001
        if score > best_score:
            best_score = score
            best = (start, end)

    if best:
        return best[0] / 1000.0, best[1] / 1000.0
    return None, None


def _pick_best_segments_all_types(segments: List[Dict[str, Any]], segment_type: str) -> List[Dict[str, Any]]:
    """Pick the best segment(s) for a given type, handling multiple segments."""
    if not segments:
        return []

    valid_segments = []
    for seg_idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            xbmc.log('[TheIntroDB] Skipping {} segment {}: not a dict'.format(segment_type, seg_idx), xbmc.LOGINFO)
            continue
        
        start = seg.get('start_ms')
        end = seg.get('end_ms')
        
        if ADDON.getSetting('debug_logging') == 'true':
            xbmc.log('[TheIntroDB] Processing {} segment {}: start_ms={}, end_ms={}'.format(segment_type, seg_idx, start, end), xbmc.LOGINFO)
        
        # Handle different segment type requirements
        if segment_type == 'intro' or segment_type == 'recap':
            # Intro/Recap: start optional (can be null), end required
            if end is None:
                if ADDON.getSetting('debug_logging') == 'true':
                    xbmc.log('[TheIntroDB] Skipping {} segment {}: end is None'.format(segment_type, seg_idx), xbmc.LOGINFO)
                continue
            if start is None:
                start = 0
        elif segment_type == 'credits' or segment_type == 'preview':
            # Credits/Preview: start required, end optional (null = end of media)
            if start is None:
                if ADDON.getSetting('debug_logging') == 'true':
                    xbmc.log('[TheIntroDB] Skipping {} segment {}: start is None'.format(segment_type, seg_idx), xbmc.LOGINFO)
                continue
            # end can be null (means end of media)
        
        if end is not None and end <= start:
            if ADDON.getSetting('debug_logging') == 'true':
                xbmc.log('[TheIntroDB] Skipping {} segment {}: end <= start ({} <= {})'.format(segment_type, seg_idx, end, start), xbmc.LOGINFO)
            continue
            
        conf = seg.get('confidence') if seg.get('confidence') is not None else 0.5
        count = seg.get('submission_count', 1)
        score = float(conf) + count * 0.001
        
        if ADDON.getSetting('debug_logging') == 'true':
            xbmc.log('[TheIntroDB] Valid {} segment {}: start={}, end={}, score={:.3f}'.format(segment_type, seg_idx, start, end, score), xbmc.LOGINFO)
        
        valid_segments.append({
            'start_ms': start,
            'end_ms': end,
            'score': score,
            'confidence': conf,
            'submission_count': count
        })
    
    if ADDON.getSetting('debug_logging') == 'true':
        xbmc.log('[TheIntroDB] {} valid {} segments found'.format(len(valid_segments), segment_type), xbmc.LOGINFO)
    
    # Sort by score (highest first) and return top segments
    valid_segments.sort(key=lambda x: x['score'], reverse=True)
    
    # Convert to seconds and return
    result_segments = []
    for seg in valid_segments:
        start_sec = seg['start_ms'] / 1000.0 if seg['start_ms'] is not None else None
        end_sec = seg['end_ms'] / 1000.0 if seg['end_ms'] is not None else None
        result_segments.append({
            'start': start_sec,
            'end': end_sec,
            'score': seg['score'],
            'type': segment_type
        })
    
    if ADDON.getSetting('debug_logging') == 'true':
        xbmc.log('[TheIntroDB] Returning {} processed {} segments'.format(len(result_segments), segment_type), xbmc.LOGINFO)
    return result_segments


def _normalize_imdb(imdb_id: Optional[str]) -> Optional[str]:
    if not imdb_id:
        return None
    s = str(imdb_id).strip()
    if not s.startswith('tt'):
        return None
    return s


def _valid_tmdb(tmdb_id: Optional[Union[str, int]]) -> bool:
    try:
        return int(str(tmdb_id)) > 0
    except (ValueError, TypeError):
        return False


def _episode_nums(season: Optional[Union[str, int]], episode: Optional[Union[str, int]]) -> Tuple[Optional[int], Optional[int]]:
    try:
        s = int(season)
        e = int(episode)
        return s, e
    except (TypeError, ValueError):
        return None, None


def _build_url(tmdb_id: Optional[Union[str, int]], imdb_id: Optional[str], season: Optional[Union[str, int]], episode: Optional[Union[str, int]], is_movie: bool, duration_ms: Optional[Union[str, int]] = None) -> Tuple[Optional[str], Optional[str]]:
    # prefer tmdb; if missing use imdb (api matches show/episode)
    duration_q = ''
    try:
        if duration_ms is not None:
            dur_int = int(duration_ms)
            if dur_int > 0:
                duration_q = '&duration_ms={}'.format(dur_int)
    except (TypeError, ValueError):
        duration_q = ''

    if tmdb_id and _valid_tmdb(tmdb_id):
        tid = str(tmdb_id).strip()
        if is_movie:
            return '{}/media?tmdb_id={}{}'.format(API_BASE, tid, duration_q), 'tmdb'
        s, e = _episode_nums(season, episode)
        if s is None or e is None or s <= 0 or e <= 0:
            return None, None
        return (
            '{}/media?tmdb_id={}&season={}&episode={}{}'.format(API_BASE, tid, s, e, duration_q),
            'tmdb',
        )

    imdb = _normalize_imdb(imdb_id)
    if not imdb:
        return None, None

    if is_movie:
        return '{}/media?imdb_id={}{}'.format(API_BASE, imdb, duration_q), 'imdb'

    s, e = _episode_nums(season, episode)
    if s is None or e is None or s <= 0 or e <= 0:
        return None, None
    return '{}/media?imdb_id={}&season={}&episode={}{}'.format(
        API_BASE, imdb, s, e, duration_q), 'imdb'


def query_intro(tmdb_id=None, imdb_id=None, season=None, episode=None, is_movie=False, duration_ms: Optional[Union[str, int]] = None):
    # returns intro start/end in seconds, or none
    if not _is_enabled():
        return None, None

    url, mode = _build_url(tmdb_id, imdb_id, season, episode, is_movie, duration_ms=duration_ms)
    if not url:
        if tmdb_id or imdb_id:
            xbmc.log(
                '[TheIntroDB] TheIntroDB: need TMDB id, or IMDb tt… id with season/episode for TV',
                xbmc.LOGINFO,
            )
        else:
            xbmc.log('[TheIntroDB] TheIntroDB: no TMDB or IMDb id', xbmc.LOGINFO)
        return None, None

    xbmc.log('[TheIntroDB] TheIntroDB query ({}): {}'.format(mode, url), xbmc.LOGINFO)

    if not _wait_rate_limit():
        return None, None

    api_key = _get_api_key()
    data = _do_request(url, api_key)
    if not data:
        return None, None

    if 'error' in data:
        xbmc.log('[TheIntroDB] TheIntroDB error: {}'.format(data['error']), xbmc.LOGINFO)
        return None, None

    intro_start, intro_end = _pick_best_segment(data.get('intro', []))
    if intro_start is not None:
        xbmc.log('[TheIntroDB] TheIntroDB intro: {:.1f}s -> {:.1f}s'.format(
            intro_start, intro_end), xbmc.LOGINFO)
    else:
        xbmc.log('[TheIntroDB] TheIntroDB: no usable intro segment', xbmc.LOGINFO)

    return intro_start, intro_end


def submit_segment(tmdb_id: Optional[Union[str, int]] = None, imdb_id: Optional[str] = None, season: Optional[Union[str, int]] = None, episode: Optional[Union[str, int]] = None,
                    is_movie: bool = False, segment: str = 'intro', start_sec: Optional[float] = None, end_sec: Optional[float] = None, video_duration_ms: Optional[Union[str, int]] = None) -> Tuple[bool, str]:
    """Submit a segment timestamp to TheIntroDB.

    Returns (success, message) tuple.
    """
    api_key = _get_api_key()
    if not api_key:
        return False, 'API key required for submissions. Set it in addon settings.'

    if not tmdb_id or not _valid_tmdb(tmdb_id):
        # fall back to imdb_id if we have one — the API can resolve it
        if not _normalize_imdb(imdb_id):
            return False, 'Need a TMDB or IMDb ID to submit.'

    if not _wait_rate_limit():
        return False, 'Rate limited. Try again later.'

    payload = {
        'segment': segment,
    }

    if tmdb_id and _valid_tmdb(tmdb_id):
        payload['tmdb_id'] = int(str(tmdb_id).strip())

    imdb = _normalize_imdb(imdb_id)
    if imdb:
        payload['imdb_id'] = imdb

    if is_movie:
        payload['type'] = 'movie'
    else:
        payload['type'] = 'tv'
        s, e = _episode_nums(season, episode)
        if s is None or e is None:
            return False, 'Season and episode are required for TV submissions.'
        payload['season'] = s
        payload['episode'] = e

    if start_sec is not None:
        payload['start_sec'] = round(float(start_sec), 1)
    else:
        payload['start_sec'] = None
    if end_sec is not None:
        payload['end_sec'] = round(float(end_sec), 1)
    else:
        payload['end_sec'] = None
    
    try:
        if video_duration_ms is not None:
            dur_int = int(video_duration_ms)
            if dur_int == 0 or (300000 <= dur_int <= 21600000):
                payload['video_duration_ms'] = dur_int
    except (TypeError, ValueError):
        pass

    url = '{}/submit'.format(API_BASE)
    xbmc.log('[TheIntroDB] Submitting segment: {} -> {}'.format(url, payload), xbmc.LOGINFO)

    body_bytes = json.dumps(payload).encode('utf-8')
    req = Request(url, data=body_bytes, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/json')
    req.add_header('User-Agent', 'TheIntroDB Kodi Addon/1.0')
    req.add_header('Authorization', 'Bearer {}'.format(api_key))

    global _rate_limit_until
    try:
        resp = urlopen(req, timeout=10)
        resp_body = resp.read().decode('utf-8')
        data = json.loads(resp_body)
        _log_resp(resp_body)
        if data.get('ok') or data.get('submissions') or data.get('submission'):
            submissions = data.get('submissions') or []
            if submissions and isinstance(submissions, list) and isinstance(submissions[0], dict):
                status = submissions[0].get('status', 'pending')
                return True, 'Submitted! Status: {}'.format(status)
            status = (data.get('submission') or {}).get('status', 'pending')
            if status:
                return True, 'Submitted! Status: {}'.format(status)
        return True, 'Submitted successfully.'
    except HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            err_data = json.loads(err_body)
            err_msg = err_data.get('error', 'HTTP {}'.format(e.code))
        except Exception:
            err_msg = 'HTTP {}'.format(e.code)
        if e.code == 429:
            retry = 300
            for header in ('X-UsageLimit-Reset', 'X-RateLimit-Reset', 'Retry-After'):
                val = e.headers.get(header)
                if val:
                    try:
                        retry = int(val)
                    except ValueError:
                        pass
                    break
            with _rate_limit_lock:
                _rate_limit_until = time.time() + retry
        xbmc.log('[TheIntroDB] Submit failed: {}'.format(err_msg), xbmc.LOGWARNING)
        return False, err_msg
    except URLError as e:
        xbmc.log('[TheIntroDB] Submit network error: {}'.format(e.reason), xbmc.LOGWARNING)
        return False, 'Network error: {}'.format(e.reason)
    except Exception as e:
        xbmc.log('[TheIntroDB] Submit error: {}'.format(e), xbmc.LOGERROR)
        return False, str(e)


def query_all_segments(tmdb_id: Optional[Union[str, int]] = None, imdb_id: Optional[str] = None, season: Optional[Union[str, int]] = None, episode: Optional[Union[str, int]] = None, is_movie: bool = False, duration_ms: Optional[Union[str, int]] = None) -> Dict[str, Any]:
    # returns dict with all segment types and their segments
    if not _is_enabled():
        return {}

    url, mode = _build_url(tmdb_id, imdb_id, season, episode, is_movie, duration_ms=duration_ms)
    if not url:
        if tmdb_id or imdb_id:
            xbmc.log(
                '[TheIntroDB] TheIntroDB: need TMDB id, or IMDb tt… id with season/episode for TV',
                xbmc.LOGINFO,
            )
        else:
            xbmc.log('[TheIntroDB] TheIntroDB: no TMDB or IMDb id', xbmc.LOGINFO)
        return {}

    xbmc.log('[TheIntroDB] TheIntroDB query all segments ({}): {}'.format(mode, url), xbmc.LOGINFO)

    if not _wait_rate_limit():
        return {}

    api_key = _get_api_key()
    data = _do_request(url, api_key)
    if not data:
        return {}

    if 'error' in data:
        xbmc.log('[TheIntroDB] TheIntroDB error: {}'.format(data['error']), xbmc.LOGINFO)
        return {}

    # Process all segment types
    all_segments = {}
    
    # Debug: Log what the API actually returned (only if debug logging is enabled)
    if ADDON.getSetting('debug_logging') == 'true':
        xbmc.log('[TheIntroDB] API response keys: {}'.format(list(data.keys())), xbmc.LOGINFO)
        xbmc.log('[TheIntroDB] Full API response (first 500 chars): {}'.format(str(data)[:500]), xbmc.LOGINFO)
        for key in data.keys():
            if key in ['intro', 'recap', 'credits', 'preview']:
                xbmc.log('[TheIntroDB] API {} raw data: {}'.format(key, len(data.get(key, []))), xbmc.LOGINFO)
    
    segment_types = ['intro', 'recap', 'credits', 'preview']
    for seg_type in segment_types:
        raw_segments = data.get(seg_type, [])
        if ADDON.getSetting('debug_logging') == 'true':
            xbmc.log('[TheIntroDB] Processing {}: {} raw segments'.format(seg_type, len(raw_segments)), xbmc.LOGINFO)
        segments = _pick_best_segments_all_types(raw_segments, seg_type)
        if segments:
            all_segments[seg_type] = segments
            if ADDON.getSetting('debug_logging') == 'true':
                xbmc.log('[TheIntroDB] TheIntroDB {}: {} valid segments'.format(seg_type, len(segments)), xbmc.LOGINFO)
        else:
            if ADDON.getSetting('debug_logging') == 'true':
                xbmc.log('[TheIntroDB] TheIntroDB {}: no valid segments'.format(seg_type), xbmc.LOGINFO)
    
    if ADDON.getSetting('debug_logging') == 'true':
        xbmc.log('[TheIntroDB] Final segments dict: {}'.format(list(all_segments.keys())), xbmc.LOGINFO)
    return all_segments
