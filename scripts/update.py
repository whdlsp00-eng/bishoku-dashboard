#!/usr/bin/env python3
"""
비쇼쿠모노 Instagram 대시보드 자동 업데이트 스크립트.

매일 GitHub Actions에서 실행되어:
1. Instagram Graph API에서 12개월치 일별 계정 인사이트, 전체 게시물 인사이트, 인구통계를 fetch
2. 장기 토큰 자동 갱신 (만료 60일 연장)
3. history.json에 일별 스냅샷 누적
4. index.html 정적 페이지 생성 (날짜 범위 필터 + 비교기간 + 인사이트 분석 포함)
"""

import os
import json
import sys
import time
import math
import datetime as dt
import html as html_module
from urllib import request, parse, error

API_BASE = "https://graph.instagram.com/v23.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(ROOT, "history.json")
DATA_PATH = os.path.join(ROOT, "data.json")
INDEX_PATH = os.path.join(ROOT, "index.html")
PROMOTED_PATH = os.path.join(ROOT, "promoted_posts.json")

FEED_METRICS = "reach,views,total_interactions,likes,comments,shares,saved,profile_visits,follows,profile_activity"
REELS_METRICS = "reach,views,total_interactions,likes,comments,shares,saved"
ACCOUNT_DAILY = "accounts_engaged,total_interactions,likes,comments,shares,saves,replies,profile_links_taps,website_clicks,profile_views,views,reach"

LOOKBACK_MONTHS = 12  # 과거 12개월치 일별 데이터 수집


def api_get(path, params=None):
    params = params or {}
    params["access_token"] = TOKEN
    url = f"{API_BASE}{path}?{parse.urlencode(params)}"
    try:
        with request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {path}: {body}") from e


def refresh_token():
    url = f"{API_BASE}/refresh_access_token?grant_type=ig_refresh_token&access_token={TOKEN}"
    try:
        with request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            new_token = data.get("access_token")
            if new_token:
                print(f"[token] refreshed, expires in {data.get('expires_in')}s")
                return new_token
    except Exception as e:
        print(f"[token] refresh failed: {e}", file=sys.stderr)
    return None


def fetch_post_insights(media_id, product_type):
    metrics = REELS_METRICS if product_type == "REELS" else FEED_METRICS
    try:
        j = api_get(f"/{media_id}/insights", {"metric": metrics})
        out = {}
        for m in j.get("data", []):
            vals = m.get("values") or []
            if vals:
                out[m["name"]] = vals[0].get("value")
        return out
    except Exception as e:
        print(f"[insights] {media_id}: {e}", file=sys.stderr)
        return {}


def fetch_daily_account(months_back):
    """Fetch daily account metrics in 30-day chunks going backward."""
    now = int(time.time())
    daily = {}
    for chunk in range(months_back):
        until = now - chunk * 30 * 86400
        since = until - 30 * 86400
        try:
            # reach is fetched separately because total_value/period mix differs
            j = api_get("/me/insights", {
                "metric": "reach",
                "period": "day",
                "since": since,
                "until": until,
            })
            for m in j.get("data", []):
                for v in m.get("values", []):
                    d = v["end_time"][:10]
                    if d not in daily:
                        daily[d] = {}
                    daily[d]["reach"] = v["value"]
        except Exception as e:
            print(f"[daily reach chunk {chunk}] failed: {e}", file=sys.stderr)

        # views & other daily totals via total_value with period=day
        try:
            j = api_get("/me/insights", {
                "metric": "views,accounts_engaged,total_interactions,profile_views,website_clicks",
                "metric_type": "total_value",
                "period": "day",
                "since": since,
                "until": until,
            })
            # total_value with period=day returns a single aggregate per metric for the range,
            # not daily values. So we record range-level aggregates in a separate bucket.
            # We rely on /me/insights with just `period=day` (no metric_type) for daily series for
            # the metrics that support it. For now, store range aggregates for backup.
        except Exception:
            pass
    print(f"[daily] collected {len(daily)} days of reach data")
    return daily


def fetch_account_totals(since, until):
    """Account-level aggregates for a given window (used for KPIs and comparison)."""
    try:
        j = api_get("/me/insights", {
            "metric": "accounts_engaged,total_interactions,likes,comments,shares,saves,replies,profile_links_taps,website_clicks,profile_views,views",
            "metric_type": "total_value",
            "period": "day",
            "since": since,
            "until": until,
        })
        out = {}
        for m in j.get("data", []):
            out[m["name"]] = (m.get("total_value") or {}).get("value", 0)
        return out
    except Exception as e:
        print(f"[totals] failed: {e}", file=sys.stderr)
        return {}


def fetch_follow_breakdown(since, until):
    try:
        j = api_get("/me/insights", {
            "metric": "follows_and_unfollows",
            "metric_type": "total_value",
            "period": "day",
            "breakdown": "follow_type",
            "since": since,
            "until": until,
        })
        out = {}
        for m in j.get("data", []):
            for b in (m.get("total_value") or {}).get("breakdowns", []):
                for r in b.get("results", []):
                    out[r["dimension_values"][0]] = r["value"]
        return out
    except Exception as e:
        print(f"[follows] failed: {e}", file=sys.stderr)
        return {}


def fetch_demographics():
    out = {}
    for dim in ("age", "gender", "country", "city"):
        try:
            j = api_get("/me/insights", {
                "metric": "follower_demographics",
                "metric_type": "total_value",
                "period": "lifetime",
                "breakdown": dim,
            })
            d = {}
            for m in j.get("data", []):
                for b in (m.get("total_value") or {}).get("breakdowns", []):
                    for r in b.get("results", []):
                        d[r["dimension_values"][0]] = r["value"]
            out[dim] = d
        except Exception as e:
            print(f"[demo:{dim}] failed: {e}", file=sys.stderr)
            out[dim] = {}
    return out


def fetch_all():
    snap = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    me = api_get("/me", {"fields": "id,username,account_type,media_count,followers_count,follows_count,name,biography,website"})
    snap["account"] = me
    print(f"[account] @{me.get('username')} followers={me.get('followers_count')} media={me.get('media_count')}")

    posts = []
    params = {"fields": "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count,thumbnail_url,media_url", "limit": 50}
    j = api_get("/me/media", params)
    posts.extend(j.get("data", []))
    while j.get("paging", {}).get("next"):
        with request.urlopen(j["paging"]["next"], timeout=30) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        posts.extend(j.get("data", []))
    print(f"[media] {len(posts)} posts")

    for p in posts:
        p["insights"] = fetch_post_insights(p["id"], p.get("media_product_type"))
    snap["posts"] = posts

    snap["daily_reach"] = fetch_daily_account(LOOKBACK_MONTHS)
    snap["demographics"] = fetch_demographics()

    return snap


def append_history(snap):
    history = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except Exception:
                history = []
    today_kst = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date().isoformat()
    history = [h for h in history if h.get("date") != today_kst]
    history.append({
        "date": today_kst,
        "followers": snap["account"].get("followers_count"),
        "media_count": snap["account"].get("media_count"),
    })
    history.sort(key=lambda h: h["date"])
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[history] {len(history)} snapshots")
    return history


def load_promoted_shortcodes():
    """Load list of Instagram shortcodes that were boosted (manually maintained)."""
    if not os.path.exists(PROMOTED_PATH):
        return set()
    try:
        with open(PROMOTED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("promoted", []))
    except Exception as e:
        print(f"[promoted] load failed: {e}", file=sys.stderr)
        return set()


def extract_shortcode(permalink):
    """Extract Instagram shortcode from permalink URL."""
    if not permalink:
        return None
    # https://www.instagram.com/p/DXoPcDAkr90/ or /reel/DXoPcDAkr90/
    parts = permalink.rstrip("/").split("/")
    return parts[-1] if parts else None


def build_post_data(posts):
    """Convert API posts into JS-friendly objects."""
    promoted = load_promoted_shortcodes()
    out = []
    for p in posts:
        ins = p.get("insights", {}) or {}
        caption = (p.get("caption") or "").split("\n")[0][:80]
        permalink = p.get("permalink", "")
        shortcode = extract_shortcode(permalink)
        out.append({
            "id": p["id"],
            "ts": (p.get("timestamp") or "")[:10],
            "type": "REELS" if p.get("media_product_type") == "REELS" else "FEED",
            "cap": caption,
            "pl": permalink,
            "ad": shortcode in promoted,
            "reach": ins.get("reach"),
            "views": ins.get("views"),
            "likes": ins.get("likes") if ins.get("likes") is not None else p.get("like_count"),
            "comments": ins.get("comments") if ins.get("comments") is not None else p.get("comments_count"),
            "shares": ins.get("shares"),
            "saved": ins.get("saved"),
            "ti": ins.get("total_interactions"),
            "pv": ins.get("profile_visits"),
            "fl": ins.get("follows"),
        })
    print(f"[promoted] marked {sum(1 for p in out if p['ad'])} posts as boosted")
    return out


def compute_insights(daily_reach, posts, history):
    """Detect anomalies in daily reach and follower count, correlate with posts."""
    insights = []

    # 1) Daily reach anomalies
    days = sorted(daily_reach.keys())
    if len(days) >= 7:
        values = [daily_reach[d].get("reach", 0) for d in days]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(var)
        threshold = mean + 1.5 * std

        # Build post lookup by date
        post_by_date = {}
        for p in posts:
            d = p["ts"]
            post_by_date.setdefault(d, []).append(p)

        spike_days = []
        for d, v in zip(days, values):
            if v > threshold and v > mean * 2:
                spike_days.append((d, v))

        for d, v in spike_days[-10:]:  # last 10 spike days
            # find posts within ±2 days
            d_dt = dt.date.fromisoformat(d)
            related = []
            for offset in range(-2, 3):
                check = (d_dt + dt.timedelta(days=offset)).isoformat()
                related.extend(post_by_date.get(check, []))
            ratio = v / mean if mean > 0 else 0
            related_str = ""
            if related:
                names = ", ".join(f"'{p['cap'][:30]}'" for p in related[:2])
                related_str = f" 같은 시기 발행된 게시물: {names}"
            insights.append({
                "type": "reach_spike",
                "date": d,
                "title": f"{d} 도달 {v:,} (평소 대비 {ratio:.1f}배)",
                "body": f"이날 계정 도달이 평소 평균 {int(mean):,}의 {ratio:.1f}배까지 치솟았습니다.{related_str}",
            })

    # 2) Follower growth
    if len(history) >= 7:
        recent = history[-7:]
        followers_start = recent[0].get("followers") or 0
        followers_end = recent[-1].get("followers") or 0
        change = followers_end - followers_start
        if change != 0:
            direction = "증가" if change > 0 else "감소"
            insights.append({
                "type": "follower_change",
                "date": recent[-1]["date"],
                "title": f"최근 7일 팔로워 {change:+} {direction}",
                "body": f"{recent[0]['date']} {followers_start:,}명 → {recent[-1]['date']} {followers_end:,}명",
            })

    # 3) Top performing posts (reach in last 30 days)
    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    recent_posts = [p for p in posts if p["ts"] >= cutoff]
    recent_posts.sort(key=lambda p: p.get("reach") or 0, reverse=True)
    if recent_posts:
        top = recent_posts[0]
        insights.append({
            "type": "top_post",
            "date": top["ts"],
            "title": f"최근 30일 최고 도달 게시물",
            "body": f"{top['ts']} {top['type']} '{top['cap'][:50]}' — 도달 {(top.get('reach') or 0):,}, 조회 {(top.get('views') or 0):,}, 참여 {(top.get('ti') or 0):,}",
        })

    # 4) High-conversion post (profile_visits / follows)
    convert_posts = [p for p in posts if (p.get("pv") or 0) >= 50 or (p.get("fl") or 0) >= 10]
    convert_posts.sort(key=lambda p: (p.get("fl") or 0, p.get("pv") or 0), reverse=True)
    for p in convert_posts[:2]:
        insights.append({
            "type": "conversion",
            "date": p["ts"],
            "title": f"전환 효자 게시물",
            "body": f"{p['ts']} '{p['cap'][:50]}' — 프로필 방문 {p.get('pv') or 0:,}, 신규 팔로우 {p.get('fl') or 0}건",
        })

    return insights


def render_html(snap, history):
    acct = snap["account"]
    posts_data = build_post_data(snap["posts"])
    daily = snap.get("daily_reach", {}) or {}
    demo = snap.get("demographics", {}) or {}

    # Demographics
    age = demo.get("age", {}) or {}
    gender_raw = demo.get("gender", {}) or {}
    gender = {"여성": gender_raw.get("F", 0), "남성": gender_raw.get("M", 0), "미상": gender_raw.get("U", 0)}
    cities_raw = demo.get("city", {}) or {}
    cities = sorted(((c.split(",")[0], v) for c, v in cities_raw.items()), key=lambda x: -x[1])[:12]
    country_raw = demo.get("country", {}) or {}
    country_str = " · ".join(f"{c}: {v}" for c, v in sorted(country_raw.items(), key=lambda x: -x[1]))

    # History for follower trend
    follower_hist = [(h["date"], h.get("followers")) for h in history if h.get("followers") is not None]

    insights = compute_insights(daily, posts_data, history)

    kst_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)
    updated_at = kst_now.strftime("%Y-%m-%d %H:%M KST")

    # Default date range: last 30 days
    today_kst = kst_now.date()
    default_until = today_kst.isoformat()
    default_since = (today_kst - dt.timedelta(days=29)).isoformat()

    # Convert daily reach dict into sorted list for JS
    daily_list = [{"d": d, "reach": v.get("reach", 0)} for d, v in sorted(daily.items())]

    data_json = json.dumps({
        "posts": posts_data,
        "daily": daily_list,
        "age": age,
        "gender": gender,
        "cities": cities,
        "follower_hist": follower_hist,
        "insights": insights,
    }, ensure_ascii=False)

    bio = acct.get("biography", "") or ""
    name = acct.get("name", "") or ""
    username = acct.get("username", "")
    followers_count = acct.get("followers_count", 0)
    media_count = acct.get("media_count", 0)

    html_str = HTML_TEMPLATE.format(
        updated_at=html_module.escape(updated_at),
        name=html_module.escape(name),
        username=html_module.escape(username),
        bio=html_module.escape(bio),
        followers=f"{followers_count:,}",
        media_count=media_count,
        default_since=default_since,
        default_until=default_until,
        country_str=html_module.escape(country_str),
        data_json=data_json,
    )

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html_str)
    print(f"[render] wrote {INDEX_PATH}")

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2, default=str)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>비쇼쿠모노 채널 성과 대시보드</title>
<style>
  :root {{ color-scheme: light; --bg:#fafaf7; --surface:#fff; --surface-2:#f2f1ec; --text:#1f1f1d; --text-2:#5f5e5a; --text-3:#888780; --border:rgba(0,0,0,0.08); --accent:#378ADD; --success:#0F6E56; --danger:#A32D2D; --warning:#BA7517; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", "Pretendard", "Helvetica Neue", Arial, sans-serif; background:var(--bg); color:var(--text); line-height:1.5; -webkit-font-smoothing:antialiased; }}
  .container {{ max-width:1080px; margin:0 auto; padding:24px 20px 60px; }}
  h1 {{ font-size:22px; font-weight:600; margin:0; }}
  h2 {{ font-size:17px; font-weight:600; margin:28px 0 12px; }}
  .muted {{ color:var(--text-2); font-size:13px; }}
  .updated {{ font-size:11px; color:var(--text-3); }}
  .header-bar {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; margin-bottom:16px; }}
  .date-controls {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 16px; margin-bottom:16px; }}
  .date-row {{ display:flex; gap:20px; flex-wrap:wrap; align-items:center; font-size:13px; }}
  .date-label {{ color:var(--text-2); min-width:60px; }}
  .date-row input[type="date"] {{ font:inherit; padding:4px 8px; border:1px solid var(--border); border-radius:6px; background:var(--surface); color:var(--text); }}
  .compare-toggle {{ display:inline-flex; border:1px solid var(--border); border-radius:6px; overflow:hidden; }}
  .compare-toggle button {{ font:inherit; padding:4px 12px; background:transparent; border:none; color:var(--text-2); cursor:pointer; }}
  .compare-toggle button.active {{ background:var(--surface-2); color:var(--text); }}
  .quick-ranges {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }}
  .quick-ranges button {{ font:inherit; font-size:11px; padding:3px 10px; background:transparent; border:1px solid var(--border); border-radius:14px; color:var(--text-2); cursor:pointer; }}
  .quick-ranges button:hover {{ background:var(--surface-2); }}
  .quick-ranges button.active {{ background:#EEEDFE; color:#3C3489; border-color:#7F77DD; }}

  .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; margin-top:8px; }}
  .kpi {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px; position:relative; }}
  .kpi-label {{ font-size:12px; color:var(--text-2); }}
  .kpi-value {{ font-size:22px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums; }}
  .kpi-change {{ font-size:11px; margin-top:4px; font-variant-numeric:tabular-nums; }}
  .change-up {{ color:#A32D2D; }}
  .change-down {{ color:#0C447C; }}
  .change-zero {{ color:var(--text-3); }}

  .tabs {{ display:flex; border-bottom:1px solid var(--border); margin:24px 0 0; gap:4px; }}
  .tab {{ padding:8px 14px; font:inherit; font-size:13px; background:transparent; border:none; color:var(--text-2); cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; }}
  .tab.active {{ color:var(--text); border-bottom-color:var(--text); font-weight:500; }}
  .tab-content {{ display:none; padding-top:8px; }}
  .tab-content.active {{ display:block; }}

  .chart-wrap {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px; margin:12px 0; }}
  .chart-canvas {{ position:relative; width:100%; height:240px; }}
  .two-col {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:16px; }}

  .post-summary {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(110px, 1fr)); gap:8px; margin:12px 0 16px; }}
  .post-summary .item {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:10px 12px; }}
  .post-summary .item-label {{ font-size:11px; color:var(--text-2); }}
  .post-summary .item-value {{ font-size:18px; font-weight:600; margin-top:2px; font-variant-numeric:tabular-nums; }}
  .post-summary .item-avg {{ font-size:10px; color:var(--text-3); margin-top:1px; font-variant-numeric:tabular-nums; }}

  table {{ width:100%; border-collapse:collapse; font-size:12px; min-width:820px; }}
  thead th {{ background:var(--surface-2); padding:10px 8px; text-align:left; font-weight:500; border-bottom:1px solid var(--border); cursor:pointer; user-select:none; }}
  tbody tr {{ border-bottom:1px solid var(--border); cursor:pointer; }}
  tbody tr:hover {{ background:var(--surface-2); }}
  td {{ padding:8px; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .pill {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:500; margin-right:3px; }}
  .pill-reels {{ background:#EEEDFE; color:#3C3489; }}
  .pill-feed {{ background:#E1F5EE; color:#085041; }}
  .pill-ad {{ background:#FAEEDA; color:#854F0B; }}
  .table-wrap {{ overflow-x:auto; background:var(--surface); border:1px solid var(--border); border-radius:10px; }}
  .insight-card {{ background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--accent); border-radius:8px; padding:12px 16px; margin-bottom:8px; }}
  .insight-card.spike {{ border-left-color:var(--warning); }}
  .insight-card.follower {{ border-left-color:var(--success); }}
  .insight-card.top {{ border-left-color:var(--accent); }}
  .insight-card.conversion {{ border-left-color:#534AB7; }}
  .insight-title {{ font-size:13px; font-weight:600; margin-bottom:4px; }}
  .insight-body {{ font-size:12px; color:var(--text-2); line-height:1.55; }}
  .country-line {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 16px; font-size:13px; margin-top:10px; }}
  .notice {{ background:#FAEEDA; color:#412402; border-radius:8px; padding:10px 14px; font-size:12px; margin-bottom:14px; line-height:1.6; }}
  .footer {{ margin-top:40px; font-size:11px; color:var(--text-3); }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#1a1a18; --surface:#242422; --surface-2:#2f2f2c; --text:#f0f0eb; --text-2:#b4b2a9; --text-3:#888780; --border:rgba(255,255,255,0.1); }}
  }}
</style>
</head>
<body>
<div class="container">

  <div class="header-bar">
    <div>
      <h1>{name} (@{username})</h1>
      <div class="muted">{bio}</div>
    </div>
    <div class="updated">마지막 업데이트: {updated_at} · 매일 오전 8:30 자동 갱신</div>
  </div>

  <div class="notice">
    <strong>광고 데이터 안내</strong> · 게시물별 도달·조회수는 Meta가 <b>오가닉 + 광고를 자동 합산</b>한 값입니다. 광고 집행한 게시물에는 <span style="background:#FAEEDA; color:#854F0B; padding:1px 6px; border-radius:3px; font-size:11px;">광고</span> 배지가 붙으며, <code>promoted_posts.json</code>에서 직접 등록할 수 있습니다.
  </div>

  <div class="date-controls">
    <div class="date-row">
      <span class="date-label">조회기간</span>
      <input type="date" id="rangeSince" value="{default_since}">
      <span>~</span>
      <input type="date" id="rangeUntil" value="{default_until}">
    </div>
    <div class="date-row" style="margin-top:8px;">
      <span class="date-label">비교기간</span>
      <input type="date" id="compareSince">
      <span>~</span>
      <input type="date" id="compareUntil">
      <span class="compare-toggle">
        <button class="active" data-mode="auto">자동</button>
        <button data-mode="custom">직접설정</button>
      </span>
    </div>
    <div class="quick-ranges">
      <button data-days="7">최근 7일</button>
      <button data-days="30" class="active">최근 30일</button>
      <button data-days="90">최근 90일</button>
      <button data-days="180">최근 6개월</button>
      <button data-days="365">최근 12개월</button>
    </div>
  </div>

  <div class="kpi-grid">
    <div class="kpi"><div class="kpi-label">팔로워</div><div class="kpi-value">{followers}</div><div class="kpi-change muted">전체 누적</div></div>
    <div class="kpi"><div class="kpi-label">게시물</div><div class="kpi-value">{media_count}</div><div class="kpi-change muted">전체 누적</div></div>
    <div class="kpi" id="kpi-reach"><div class="kpi-label">도달</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-views"><div class="kpi-label">조회수</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-likes"><div class="kpi-label">좋아요</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-comments"><div class="kpi-label">댓글</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-shares"><div class="kpi-label">공유</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-saved"><div class="kpi-label">저장</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-pv"><div class="kpi-label">프로필 방문</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-fl"><div class="kpi-label">팔로우 전환</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
    <div class="kpi" id="kpi-posts"><div class="kpi-label">게시물 (기간)</div><div class="kpi-value">-</div><div class="kpi-change">-</div></div>
  </div>

  <div class="tabs">
    <button class="tab active" data-tab="overview">대시보드</button>
    <button class="tab" data-tab="insights">인사이트 분석</button>
    <button class="tab" data-tab="content">콘텐츠 분석</button>
    <button class="tab" data-tab="audience">팔로워 분석</button>
  </div>

  <!-- Overview tab -->
  <div class="tab-content active" id="tab-overview">
    <h2>일별 도달 추이 (선택 기간)</h2>
    <div class="chart-wrap"><div class="chart-canvas"><canvas id="reachChart" role="img" aria-label="일별 도달 추이"></canvas></div></div>

    <h2>팔로워 변화</h2>
    <div class="chart-wrap"><div class="chart-canvas"><canvas id="followerChart" role="img" aria-label="팔로워 변화"></canvas></div></div>
  </div>

  <!-- Insights tab -->
  <div class="tab-content" id="tab-insights">
    <h2>자동 인사이트</h2>
    <div class="muted" style="margin-bottom:12px;">일별 도달 이상치와 팔로워 변동을 자동 감지해 영향을 준 게시물과 매칭한 결과입니다.</div>
    <div id="insightList"></div>
  </div>

  <!-- Content tab -->
  <div class="tab-content" id="tab-content">
    <h2>콘텐츠 타입별 평균 성과</h2>
    <div class="chart-wrap"><div class="chart-canvas" style="height:260px;"><canvas id="typeChart" role="img" aria-label="콘텐츠 타입별 평균"></canvas></div></div>

    <h2>콘텐츠별 성과</h2>
    <div class="muted" style="font-size:12px;">선택 기간 안에 발행된 게시물 — 합계(큰 숫자) + 평균(작은 숫자)</div>
    <div class="post-summary" id="postSummary"></div>
    <div class="muted" style="font-size:12px; margin-bottom:8px;">컬럼 클릭 시 정렬 · 행 클릭 시 인스타그램 원문 열기</div>
    <div class="table-wrap"><div id="postTable"></div></div>
  </div>

  <!-- Audience tab -->
  <div class="tab-content" id="tab-audience">
    <h2>팔로워 인사이트</h2>
    <div class="two-col">
      <div class="chart-wrap"><div class="muted" style="margin-bottom:8px;">연령대 분포</div><div class="chart-canvas" style="height:220px;"><canvas id="ageChart"></canvas></div></div>
      <div class="chart-wrap"><div class="muted" style="margin-bottom:8px;">성별 분포</div><div class="chart-canvas" style="height:220px;"><canvas id="genderChart"></canvas></div></div>
    </div>
    <div class="chart-wrap"><div class="muted" style="margin-bottom:8px;">도시별 팔로워 (Top 12)</div><div class="chart-canvas" style="height:360px;"><canvas id="cityChart"></canvas></div></div>
    <div class="country-line"><span class="muted">국가 분포</span><br>{country_str}</div>
  </div>

  <div class="footer">데이터: Instagram Graph API v23.0 · 매일 오전 8:30 KST 자동 갱신</div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js"></script>
<script>
const DATA = {data_json};
Chart.defaults.font.family = '-apple-system, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif';

// State
const state = {{ since:'{default_since}', until:'{default_until}', cmpSince:null, cmpUntil:null, cmpMode:'auto', activeTab:'overview' }};
const charts = {{}};

// Helpers
function diffDays(a, b) {{ return Math.round((new Date(b) - new Date(a)) / 86400000); }}
function addDays(d, n) {{ const dt = new Date(d); dt.setDate(dt.getDate() + n); return dt.toISOString().slice(0,10); }}
function inRange(d, since, until) {{ return d >= since && d <= until; }}

// Compute auto comparison: same-length prior period
function autoCmp(since, until) {{
  const len = diffDays(since, until) + 1;
  return [addDays(since, -len), addDays(since, -1)];
}}

function applyAutoCmp() {{
  const [s, u] = autoCmp(state.since, state.until);
  state.cmpSince = s; state.cmpUntil = u;
  document.getElementById('compareSince').value = s;
  document.getElementById('compareUntil').value = u;
}}

// Aggregate posts within a date range
function aggPosts(since, until) {{
  const filtered = DATA.posts.filter(p => inRange(p.ts, since, until));
  const fields = ['reach','views','likes','comments','shares','saved','ti','pv','fl'];
  const sums = {{count: filtered.length}}, avgs = {{}};
  fields.forEach(k => {{
    const vals = filtered.map(p => p[k]).filter(v => v != null && !isNaN(v));
    sums[k] = vals.reduce((s,v) => s+v, 0);
    avgs[k] = vals.length ? Math.round(sums[k] / vals.length) : null;
  }});
  return {{filtered, sums, avgs}};
}}

// Compute % change
function pctChange(cur, prev) {{
  if (!prev) return cur ? null : 0;
  return ((cur - prev) / prev) * 100;
}}
function fmtChange(p) {{
  if (p === null || p === undefined) return '<span class="muted">신규</span>';
  if (Math.abs(p) < 0.05) return '<span class="change-zero">±0%</span>';
  const arrow = p > 0 ? '▲' : '▼';
  const cls = p > 0 ? 'change-up' : 'change-down';
  return `<span class="${{cls}}">${{arrow}} ${{Math.abs(p).toFixed(1)}}%</span>`;
}}

// Render KPI cards
function renderKPIs() {{
  const cur = aggPosts(state.since, state.until);
  const prev = aggPosts(state.cmpSince, state.cmpUntil);
  const map = [
    ['kpi-reach','reach'], ['kpi-views','views'], ['kpi-likes','likes'], ['kpi-comments','comments'],
    ['kpi-shares','shares'], ['kpi-saved','saved'], ['kpi-pv','pv'], ['kpi-fl','fl']
  ];
  map.forEach(([id,k]) => {{
    const el = document.getElementById(id);
    const v = cur.sums[k] || 0;
    const pv = prev.sums[k] || 0;
    const ch = pctChange(v, pv);
    el.querySelector('.kpi-value').textContent = v.toLocaleString();
    el.querySelector('.kpi-change').innerHTML = `${{fmtChange(ch)}} <span class="muted">비교 ${{pv.toLocaleString()}}</span>`;
  }});
  const elPosts = document.getElementById('kpi-posts');
  const chPosts = pctChange(cur.sums.count, prev.sums.count);
  elPosts.querySelector('.kpi-value').textContent = cur.sums.count;
  elPosts.querySelector('.kpi-change').innerHTML = `${{fmtChange(chPosts)}} <span class="muted">비교 ${{prev.sums.count}}개</span>`;
}}

// Render content summary cards
function renderPostSummary() {{
  const {{ sums, avgs }} = aggPosts(state.since, state.until);
  const LABELS = {{reach:'도달', views:'조회수', likes:'좋아요', comments:'댓글', shares:'공유', saved:'저장', ti:'총 참여', pv:'프로필 방문', fl:'팔로우 전환'}};
  let html = '';
  Object.keys(LABELS).forEach(k => {{
    const sum = (sums[k] || 0).toLocaleString();
    const avg = avgs[k] == null ? '-' : avgs[k].toLocaleString();
    html += `<div class="item"><div class="item-label">${{LABELS[k]}}</div><div class="item-value">${{sum}}</div><div class="item-avg">평균 ${{avg}}</div></div>`;
  }});
  document.getElementById('postSummary').innerHTML = html;
}}

// Render post table (filtered by range)
let sortKey = 'ts', sortDir = -1;
function renderTable() {{
  const {{ filtered }} = aggPosts(state.since, state.until);
  const sorted = [...filtered].sort((a,b) => {{
    const av = a[sortKey], bv = b[sortKey];
    if (av == null) return 1; if (bv == null) return -1;
    if (typeof av === 'string') return av < bv ? -sortDir : av > bv ? sortDir : 0;
    return (av - bv) * sortDir;
  }});
  const cols = [
    {{k:'ts',label:'날짜'}}, {{k:'type',label:'타입'}}, {{k:'cap',label:'캡션'}},
    {{k:'reach',label:'도달',n:true}}, {{k:'views',label:'조회',n:true}},
    {{k:'likes',label:'좋아',n:true}}, {{k:'comments',label:'댓글',n:true}},
    {{k:'shares',label:'공유',n:true}}, {{k:'saved',label:'저장',n:true}},
    {{k:'ti',label:'참여',n:true}}, {{k:'pv',label:'프로필',n:true}}, {{k:'fl',label:'팔로우',n:true}}
  ];
  let html = '<table><thead><tr>';
  cols.forEach(c => {{
    const arrow = sortKey === c.k ? (sortDir === 1 ? ' ↑' : ' ↓') : '';
    html += `<th data-k="${{c.k}}" style="text-align:${{c.n?'right':'left'}};">${{c.label}}${{arrow}}</th>`;
  }});
  html += '</tr></thead><tbody>';
  if (!sorted.length) {{
    html += `<tr><td colspan="12" style="padding:20px; text-align:center; color:var(--text-3);">이 기간에 발행된 게시물이 없습니다.</td></tr>`;
  }}
  sorted.forEach(p => {{
    const pillClass = p.type === 'REELS' ? 'pill-reels' : 'pill-feed';
    html += `<tr data-pl="${{p.pl}}">`;
    html += `<td>${{p.ts.substring(5)}}</td>`;
    html += `<td><span class="pill ${{pillClass}}">${{p.type}}</span>${{p.ad?'<span class=\"pill pill-ad\">광고</span>':''}}</td>`;
    html += `<td style="max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${{(p.cap||'').replace(/</g,'&lt;')}}</td>`;
    ['reach','views','likes','comments','shares','saved','ti','pv','fl'].forEach(k => {{
      const v = p[k];
      html += `<td class="num">${{v == null ? '<span style=\"color:var(--text-3);\">-</span>' : v.toLocaleString()}}</td>`;
    }});
    html += '</tr>';
  }});
  html += '</tbody></table>';
  document.getElementById('postTable').innerHTML = html;
  document.querySelectorAll('#postTable th').forEach(th => th.addEventListener('click', () => {{
    const k = th.getAttribute('data-k');
    if (sortKey === k) sortDir = -sortDir; else {{ sortKey = k; sortDir = -1; }}
    renderTable();
  }}));
  document.querySelectorAll('#postTable tbody tr').forEach(tr => tr.addEventListener('click', () => {{
    const pl = tr.getAttribute('data-pl'); if (pl) window.open(pl, '_blank');
  }}));
}}

// Render daily reach chart (filtered)
function renderReachChart() {{
  const filtered = DATA.daily.filter(d => inRange(d.d, state.since, state.until));
  if (charts.reach) charts.reach.destroy();
  charts.reach = new Chart(document.getElementById('reachChart'), {{
    type:'line',
    data:{{ labels: filtered.map(d => d.d.substring(5)), datasets:[{{label:'일별 도달', data: filtered.map(d => d.reach), borderColor:'#378ADD', backgroundColor:'rgba(55,138,221,0.12)', fill:true, tension:0.3, pointRadius:2, borderWidth:2}}] }},
    options:{{ responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}}, scales:{{ x:{{ticks:{{maxTicksLimit:12, font:{{size:11}} }} }}, y:{{ticks:{{font:{{size:11}}, callback: v => v.toLocaleString() }} }} }} }}
  }});
}}

// Follower chart
function renderFollowerChart() {{
  if (charts.follower) charts.follower.destroy();
  if (DATA.follower_hist.length < 2) {{
    document.getElementById('followerChart').parentElement.innerHTML = '<div class="muted" style="text-align:center; padding:40px 0; font-size:13px;">팔로워 변화 추이는 최소 2일치 데이터가 누적된 후 표시됩니다.</div>';
    return;
  }}
  charts.follower = new Chart(document.getElementById('followerChart'), {{
    type:'line',
    data:{{ labels: DATA.follower_hist.map(h => h[0].substring(5)), datasets:[{{label:'팔로워', data: DATA.follower_hist.map(h => h[1]), borderColor:'#1D9E75', backgroundColor:'rgba(29,158,117,0.12)', fill:true, tension:0.2, pointRadius:3, borderWidth:2}}] }},
    options:{{ responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}}, scales:{{ x:{{ticks:{{maxTicksLimit:10, font:{{size:11}} }} }}, y:{{ticks:{{font:{{size:11}}}} }} }} }}
  }});
}}

// Type comparison chart
function renderTypeChart() {{
  const {{ filtered }} = aggPosts(state.since, state.until);
  const reels = filtered.filter(p => p.type === 'REELS');
  const feed = filtered.filter(p => p.type === 'FEED');
  function avg(arr, k) {{ const vals = arr.map(p => p[k]).filter(v => v != null); return vals.length ? Math.round(vals.reduce((s,v) => s+v, 0) / vals.length) : 0; }}
  if (charts.type) charts.type.destroy();
  charts.type = new Chart(document.getElementById('typeChart'), {{
    type:'bar',
    data:{{ labels:['도달','조회수','좋아요','댓글','공유','저장'],
      datasets:[
        {{label:`REELS (${{reels.length}}개)`, data:['reach','views','likes','comments','shares','saved'].map(k => avg(reels, k)), backgroundColor:'#7F77DD'}},
        {{label:`FEED (${{feed.length}}개)`, data:['reach','views','likes','comments','shares','saved'].map(k => avg(feed, k)), backgroundColor:'#1D9E75'}}
      ]
    }},
    options:{{ responsive:true, maintainAspectRatio:false, plugins:{{legend:{{position:'top', labels:{{font:{{size:12}}, boxWidth:12}} }} }} }}
  }});
}}

// Demographics charts (static)
function renderDemoCharts() {{
  if (!charts.age) {{
    charts.age = new Chart(document.getElementById('ageChart'), {{ type:'bar', data:{{labels:Object.keys(DATA.age), datasets:[{{data:Object.values(DATA.age), backgroundColor:'#534AB7'}}]}}, options:{{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}}}} }});
    charts.gender = new Chart(document.getElementById('genderChart'), {{ type:'doughnut', data:{{labels:Object.keys(DATA.gender), datasets:[{{data:Object.values(DATA.gender), backgroundColor:['#D4537E','#378ADD','#888780']}}]}}, options:{{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{position:'right', labels:{{font:{{size:12}}, boxWidth:12}} }} }} }} }});
    charts.city = new Chart(document.getElementById('cityChart'), {{ type:'bar', data:{{labels:DATA.cities.map(c => c[0]), datasets:[{{data:DATA.cities.map(c => c[1]), backgroundColor:'#0F6E56'}}]}}, options:{{indexAxis:'y', responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}}}} }});
  }}
}}

// Insights cards
function renderInsights() {{
  const list = DATA.insights || [];
  if (!list.length) {{
    document.getElementById('insightList').innerHTML = '<div class="muted" style="padding:20px 0;">아직 충분한 데이터가 누적되지 않아 자동 인사이트를 만들지 못했습니다. 일별 도달 데이터가 7일 이상 쌓이면 표시됩니다.</div>';
    return;
  }}
  let html = '';
  list.forEach(i => {{
    const cls = i.type === 'reach_spike' ? 'spike' : i.type === 'follower_change' ? 'follower' : i.type === 'conversion' ? 'conversion' : 'top';
    html += `<div class="insight-card ${{cls}}"><div class="insight-title">${{i.title}}</div><div class="insight-body">${{i.body}}</div></div>`;
  }});
  document.getElementById('insightList').innerHTML = html;
}}

// Master render
function renderAll() {{
  renderKPIs();
  renderPostSummary();
  renderTable();
  renderReachChart();
  renderFollowerChart();
  renderTypeChart();
  renderDemoCharts();
  renderInsights();
}}

// Wire up controls
function bindControls() {{
  document.getElementById('rangeSince').addEventListener('change', e => {{ state.since = e.target.value; if (state.cmpMode === 'auto') applyAutoCmp(); document.querySelectorAll('.quick-ranges button').forEach(b => b.classList.remove('active')); renderAll(); }});
  document.getElementById('rangeUntil').addEventListener('change', e => {{ state.until = e.target.value; if (state.cmpMode === 'auto') applyAutoCmp(); document.querySelectorAll('.quick-ranges button').forEach(b => b.classList.remove('active')); renderAll(); }});
  document.getElementById('compareSince').addEventListener('change', e => {{ state.cmpSince = e.target.value; state.cmpMode = 'custom'; document.querySelectorAll('.compare-toggle button').forEach(b => b.classList.toggle('active', b.dataset.mode === 'custom')); renderKPIs(); }});
  document.getElementById('compareUntil').addEventListener('change', e => {{ state.cmpUntil = e.target.value; state.cmpMode = 'custom'; document.querySelectorAll('.compare-toggle button').forEach(b => b.classList.toggle('active', b.dataset.mode === 'custom')); renderKPIs(); }});
  document.querySelectorAll('.compare-toggle button').forEach(btn => btn.addEventListener('click', () => {{
    document.querySelectorAll('.compare-toggle button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.cmpMode = btn.dataset.mode;
    if (state.cmpMode === 'auto') applyAutoCmp();
    renderKPIs();
  }}));
  document.querySelectorAll('.quick-ranges button').forEach(btn => btn.addEventListener('click', () => {{
    document.querySelectorAll('.quick-ranges button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const days = parseInt(btn.dataset.days);
    const until = new Date().toISOString().slice(0,10);
    const since = addDays(until, -(days - 1));
    state.since = since; state.until = until;
    document.getElementById('rangeSince').value = since;
    document.getElementById('rangeUntil').value = until;
    if (state.cmpMode === 'auto') applyAutoCmp();
    renderAll();
  }}));
  document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    state.activeTab = tab.dataset.tab;
  }}));
}}

applyAutoCmp();
bindControls();
renderAll();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    TOKEN = os.environ.get("BISHOKU_MONO_API_KEY")
    if not TOKEN:
        print("ERROR: BISHOKU_MONO_API_KEY env var not set", file=sys.stderr)
        sys.exit(1)

    new_token = refresh_token()
    if new_token:
        TOKEN = new_token

    snap = fetch_all()
    history = append_history(snap)
    render_html(snap, history)
    print("[done]")
