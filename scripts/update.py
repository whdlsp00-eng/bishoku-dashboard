#!/usr/bin/env python3
"""
비쇼쿠모노 Instagram 대시보드 자동 업데이트 스크립트

매일 GitHub Actions에서 실행되어:
1. Instagram Graph API에서 최신 데이터 fetch
2. 장기 토큰 자동 갱신 (만료 60일 연장)
3. history.json에 일별 스냅샷 누적
4. index.html 정적 페이지 생성
"""

import os
import json
import sys
import time
import datetime as dt
from urllib import request, parse, error

API_BASE = "https://graph.instagram.com/v23.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(ROOT, "history.json")
DATA_PATH = os.path.join(ROOT, "data.json")
INDEX_PATH = os.path.join(ROOT, "index.html")

# Metric sets per media product type
FEED_METRICS = "reach,views,total_interactions,likes,comments,shares,saved,profile_visits,follows,profile_activity"
REELS_METRICS = "reach,views,total_interactions,likes,comments,shares,saved"

# Account metric sets
ACCOUNT_TOTALS = "accounts_engaged,total_interactions,likes,comments,shares,saves,replies,profile_links_taps,website_clicks,profile_views,views"


def api_get(path, params=None):
    """GET request to Instagram Graph API, returning parsed JSON."""
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
    """Refresh long-lived access token (extends expiry by 60 days)."""
    url = f"{API_BASE}/refresh_access_token?grant_type=ig_refresh_token&access_token={TOKEN}"
    try:
        with request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            new_token = data.get("access_token")
            expires_in = data.get("expires_in")
            if new_token:
                print(f"[token] refreshed, expires in {expires_in}s ({expires_in/86400:.1f} days)")
                return new_token
    except Exception as e:
        print(f"[token] refresh failed: {e}", file=sys.stderr)
    return None


def fetch_insights(media_id, product_type):
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


def fetch_all():
    """Fetch all data needed for the dashboard."""
    snap = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    # Account info
    me = api_get("/me", {"fields": "id,username,account_type,media_count,followers_count,follows_count,name,biography,website"})
    snap["account"] = me
    print(f"[account] @{me.get('username')} followers={me.get('followers_count')} media={me.get('media_count')}")

    # Media list (paginate if needed)
    posts = []
    next_url = None
    params = {"fields": "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count,thumbnail_url,media_url", "limit": 50}
    j = api_get("/me/media", params)
    posts.extend(j.get("data", []))
    while j.get("paging", {}).get("next"):
        next_url = j["paging"]["next"]
        # next URL already has access_token, just fetch
        with request.urlopen(next_url, timeout=30) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        posts.extend(j.get("data", []))
    print(f"[media] fetched {len(posts)} posts")

    # Per-media insights
    for p in posts:
        p["insights"] = fetch_insights(p["id"], p.get("media_product_type"))
    snap["posts"] = posts

    # Account-level insights (last 30 days)
    until = int(time.time())
    since = until - 30 * 86400
    try:
        j = api_get("/me/insights", {
            "metric": ACCOUNT_TOTALS,
            "metric_type": "total_value",
            "period": "day",
            "since": since,
            "until": until,
        })
        totals = {}
        for m in j.get("data", []):
            totals[m["name"]] = (m.get("total_value") or {}).get("value")
        snap["account_30d"] = totals
        print(f"[account_30d] {totals}")
    except Exception as e:
        print(f"[account_30d] failed: {e}", file=sys.stderr)
        snap["account_30d"] = {}

    # Daily reach time series
    try:
        j = api_get("/me/insights", {"metric": "reach", "period": "day", "since": since, "until": until})
        daily = []
        for m in j.get("data", []):
            for v in m.get("values", []):
                daily.append({"date": v["end_time"][:10], "reach": v["value"]})
        snap["daily_reach"] = daily
    except Exception as e:
        print(f"[daily_reach] failed: {e}", file=sys.stderr)
        snap["daily_reach"] = []

    # Follows / unfollows (30 days)
    try:
        j = api_get("/me/insights", {
            "metric": "follows_and_unfollows",
            "metric_type": "total_value",
            "period": "day",
            "breakdown": "follow_type",
            "since": since,
            "until": until,
        })
        fu = {}
        for m in j.get("data", []):
            for b in (m.get("total_value") or {}).get("breakdowns", []):
                for r in b.get("results", []):
                    fu[r["dimension_values"][0]] = r["value"]
        snap["follow_breakdown"] = fu
    except Exception as e:
        print(f"[follow_breakdown] failed: {e}", file=sys.stderr)
        snap["follow_breakdown"] = {}

    # Demographics
    snap["demographics"] = {}
    for dim in ("age", "gender", "country", "city"):
        try:
            j = api_get("/me/insights", {
                "metric": "follower_demographics",
                "metric_type": "total_value",
                "period": "lifetime",
                "breakdown": dim,
            })
            out = {}
            for m in j.get("data", []):
                for b in (m.get("total_value") or {}).get("breakdowns", []):
                    for r in b.get("results", []):
                        out[r["dimension_values"][0]] = r["value"]
            snap["demographics"][dim] = out
        except Exception as e:
            print(f"[demo:{dim}] failed: {e}", file=sys.stderr)
            snap["demographics"][dim] = {}

    return snap


def append_history(snap):
    """Append today's snapshot of key totals to history.json."""
    history = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except Exception:
                history = []
    today_kst = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date().isoformat()
    # Replace existing entry for today, or append
    history = [h for h in history if h.get("date") != today_kst]
    history.append({
        "date": today_kst,
        "followers": snap["account"].get("followers_count"),
        "media_count": snap["account"].get("media_count"),
        "account_30d": snap.get("account_30d", {}),
        "follow_breakdown": snap.get("follow_breakdown", {}),
    })
    history.sort(key=lambda h: h["date"])
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[history] now {len(history)} snapshots")
    return history


def render_html(snap, history):
    """Render the dashboard HTML using the snapshot + history."""
    import html as html_module

    acct = snap["account"]
    posts = snap["posts"]
    a30 = snap.get("account_30d", {}) or {}
    fb = snap.get("follow_breakdown", {}) or {}
    demo = snap.get("demographics", {}) or {}
    daily = snap.get("daily_reach", [])

    # Day-over-day deltas
    delta = {}
    if len(history) >= 2:
        prev = history[-2]
        cur = history[-1]
        delta["followers"] = (cur.get("followers") or 0) - (prev.get("followers") or 0)
        delta["media_count"] = (cur.get("media_count") or 0) - (prev.get("media_count") or 0)

    # Build posts data for JS
    posts_js = []
    for p in posts:
        ins = p.get("insights", {}) or {}
        caption = (p.get("caption") or "").split("\n")[0][:80]
        posts_js.append({
            "id": p["id"],
            "ts": (p.get("timestamp") or "")[:10],
            "type": "REELS" if p.get("media_product_type") == "REELS" else "FEED",
            "cap": caption,
            "pl": p.get("permalink", ""),
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

    # KST timestamp
    kst_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)
    updated_at = kst_now.strftime("%Y-%m-%d %H:%M KST")

    # Korean demographics
    age = demo.get("age", {}) or {}
    gender_raw = demo.get("gender", {}) or {}
    gender = {
        "여성": gender_raw.get("F", 0),
        "남성": gender_raw.get("M", 0),
        "미상": gender_raw.get("U", 0),
    }
    cities_raw = demo.get("city", {}) or {}
    cities_sorted = sorted(cities_raw.items(), key=lambda x: -x[1])[:12]
    # Strip subdivision suffix to keep labels short
    cities = [(c.split(",")[0], v) for c, v in cities_sorted]

    country_raw = demo.get("country", {}) or {}
    country_sorted = sorted(country_raw.items(), key=lambda x: -x[1])
    country_str = " · ".join(f"{c}: {v}" for c, v in country_sorted)

    # History for follower trend
    follower_hist = [(h["date"], h.get("followers")) for h in history if h.get("followers") is not None]

    # Compute follower growth deltas
    growth_arrow = ""
    if "followers" in delta:
        d = delta["followers"]
        if d > 0:
            growth_arrow = f'<span style="color:#0F6E56;">+{d}</span>'
        elif d < 0:
            growth_arrow = f'<span style="color:#A32D2D;">{d}</span>'
        else:
            growth_arrow = '<span style="color:#888780;">±0</span>'

    new_followers = fb.get("FOLLOWER", 0)
    unfollows = fb.get("NON_FOLLOWER", 0)

    # JSON-encode data for embedding
    data_json = json.dumps({
        "posts": posts_js,
        "daily": daily,
        "age": age,
        "gender": gender,
        "cities": cities,
        "follower_hist": follower_hist,
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
        growth_arrow=growth_arrow,
        new_followers=new_followers,
        unfollows=unfollows,
        views=f"{a30.get('views', 0) or 0:,}",
        engaged=f"{a30.get('accounts_engaged', 0) or 0:,}",
        interactions=f"{a30.get('total_interactions', 0) or 0:,}",
        profile_views=f"{a30.get('profile_views', 0) or 0:,}",
        website_clicks=f"{a30.get('website_clicks', 0) or 0:,}",
        likes_30d=f"{a30.get('likes', 0) or 0:,}",
        comments_30d=f"{a30.get('comments', 0) or 0:,}",
        shares_30d=f"{a30.get('shares', 0) or 0:,}",
        saves_30d=f"{a30.get('saves', 0) or 0:,}",
        country_str=html_module.escape(country_str),
        data_json=data_json,
    )

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html_str)
    print(f"[render] wrote {INDEX_PATH}")

    # Also save raw snapshot for debugging
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
  :root {{
    color-scheme: light;
    --bg: #fafaf7;
    --surface: #ffffff;
    --surface-2: #f2f1ec;
    --text: #1f1f1d;
    --text-2: #5f5e5a;
    --text-3: #888780;
    --border: rgba(0,0,0,0.08);
    --accent: #378ADD;
    --success: #0F6E56;
    --danger: #A32D2D;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", "Pretendard", "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .container {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 60px; }}
  h1 {{ font-size: 24px; font-weight: 600; margin: 0; }}
  h2 {{ font-size: 18px; font-weight: 600; margin: 32px 0 12px; }}
  .muted {{ color: var(--text-2); font-size: 13px; }}
  .header {{ display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }}
  .updated {{ font-size: 12px; color: var(--text-3); }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-top: 20px; }}
  .kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
  .kpi-label {{ font-size: 12px; color: var(--text-2); }}
  .kpi-value {{ font-size: 24px; font-weight: 600; margin-top: 4px; }}
  .kpi-sub {{ font-size: 11px; color: var(--text-3); margin-top: 2px; }}
  .chart-wrap {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin: 12px 0; }}
  .chart-canvas {{ position: relative; width: 100%; height: 240px; }}
  .two-col {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
  .country-line {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 800px; }}
  thead th {{ background: var(--surface-2); padding: 10px 8px; text-align: left; font-weight: 500; border-bottom: 1px solid var(--border); cursor: pointer; user-select: none; }}
  thead th:hover {{ background: #e8e7e2; }}
  tbody tr {{ border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.1s; }}
  tbody tr:hover {{ background: var(--surface-2); }}
  td {{ padding: 8px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 500; }}
  .pill-reels {{ background: #EEEDFE; color: #3C3489; }}
  .pill-feed {{ background: #E1F5EE; color: #085041; }}
  .table-wrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }}
  .footer {{ margin-top: 40px; font-size: 11px; color: var(--text-3); }}
  .footer a {{ color: var(--text-2); }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1a18;
      --surface: #242422;
      --surface-2: #2f2f2c;
      --text: #f0f0eb;
      --text-2: #b4b2a9;
      --text-3: #888780;
      --border: rgba(255,255,255,0.1);
    }}
    color-scheme: dark;
  }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>{name} (@{username})</h1>
    <div class="muted">{bio}</div>
    <div class="updated">마지막 업데이트: {updated_at} · 매일 오전 8:30 자동 갱신</div>
  </div>

  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label">팔로워</div>
      <div class="kpi-value">{followers}</div>
      <div class="kpi-sub">전일 대비 {growth_arrow}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">게시물</div>
      <div class="kpi-value">{media_count}</div>
      <div class="kpi-sub">누적</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">조회수 (30일)</div>
      <div class="kpi-value">{views}</div>
      <div class="kpi-sub">전체 콘텐츠 합</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">참여 계정 (30일)</div>
      <div class="kpi-value">{engaged}</div>
      <div class="kpi-sub">unique accounts</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">총 상호작용 (30일)</div>
      <div class="kpi-value">{interactions}</div>
      <div class="kpi-sub">좋아요 {likes_30d} · 저장 {saves_30d} · 공유 {shares_30d} · 댓글 {comments_30d}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">프로필 방문 (30일)</div>
      <div class="kpi-value">{profile_views}</div>
      <div class="kpi-sub">웹사이트 클릭 {website_clicks}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">신규 팔로우 (30일)</div>
      <div class="kpi-value">{new_followers}</div>
      <div class="kpi-sub">언팔로우 {unfollows}</div>
    </div>
  </div>

  <h2>일별 도달 추이</h2>
  <div class="chart-wrap"><div class="chart-canvas"><canvas id="reachChart" role="img" aria-label="일별 도달 라인 차트"></canvas></div></div>

  <h2>팔로워 변화</h2>
  <div class="chart-wrap"><div class="chart-canvas"><canvas id="followerChart" role="img" aria-label="일별 팔로워 수 라인 차트"></canvas></div></div>

  <h2>콘텐츠 타입별 평균 성과</h2>
  <div class="chart-wrap"><div class="chart-canvas" style="height:260px;"><canvas id="typeChart" role="img" aria-label="REELS와 FEED 평균 성과 비교"></canvas></div></div>

  <h2>콘텐츠별 성과</h2>
  <div class="muted" style="font-size:12px; margin-bottom:8px;">컬럼 클릭 시 정렬 · 행 클릭 시 인스타그램 원문 열기</div>
  <div class="table-wrap"><div id="postTable"></div></div>

  <h2>팔로워 인사이트</h2>
  <div class="two-col">
    <div class="chart-wrap"><div class="muted" style="margin-bottom:8px;">연령대 분포</div><div class="chart-canvas" style="height:220px;"><canvas id="ageChart" role="img" aria-label="연령대 분포 막대 차트"></canvas></div></div>
    <div class="chart-wrap"><div class="muted" style="margin-bottom:8px;">성별 분포</div><div class="chart-canvas" style="height:220px;"><canvas id="genderChart" role="img" aria-label="성별 분포 도넛 차트"></canvas></div></div>
  </div>
  <div class="chart-wrap"><div class="muted" style="margin-bottom:8px;">도시별 팔로워 (Top 12)</div><div class="chart-canvas" style="height:360px;"><canvas id="cityChart" role="img" aria-label="도시별 팔로워 가로 막대 차트"></canvas></div></div>
  <div class="country-line"><span class="muted">국가 분포</span><br>{country_str}</div>

  <div class="footer">
    데이터: Instagram Graph API v23.0 · 매일 오전 8:30 KST GitHub Actions로 자동 갱신 · <a href="https://github.com/" target="_blank">소스 보기</a>
  </div>

</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js"></script>
<script>
const DATA = {data_json};
Chart.defaults.font.family = '-apple-system, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif';
Chart.defaults.color = getComputedStyle(document.body).getPropertyValue('--text-2').trim() || '#5f5e5a';

// Daily reach
new Chart(document.getElementById('reachChart'), {{
  type: 'line',
  data: {{
    labels: DATA.daily.map(d => d.date.substring(5)),
    datasets: [{{ label: '일별 도달', data: DATA.daily.map(d => d.reach), borderColor: '#378ADD', backgroundColor: 'rgba(55,138,221,0.12)', fill: true, tension: 0.3, pointRadius: 2, borderWidth: 2 }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ maxTicksLimit: 8, font:{{size:11}} }} }}, y: {{ ticks: {{ font:{{size:11}}, callback: v => v.toLocaleString() }} }} }} }}
}});

// Follower history
if (DATA.follower_hist.length >= 2) {{
  new Chart(document.getElementById('followerChart'), {{
    type: 'line',
    data: {{
      labels: DATA.follower_hist.map(h => h[0].substring(5)),
      datasets: [{{ label: '팔로워', data: DATA.follower_hist.map(h => h[1]), borderColor: '#1D9E75', backgroundColor: 'rgba(29,158,117,0.12)', fill: true, tension: 0.2, pointRadius: 3, borderWidth: 2 }}]
    }},
    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ maxTicksLimit: 10, font:{{size:11}} }} }}, y: {{ ticks: {{ font:{{size:11}} }} }} }} }}
  }});
}} else {{
  document.getElementById('followerChart').parentElement.innerHTML = '<div class="muted" style="text-align:center; padding:40px 0; font-size:13px;">팔로워 변화 추이는 최소 2일치 데이터가 누적된 후 표시됩니다.</div>';
}}

// Content type comparison
const reels = DATA.posts.filter(p => p.type === 'REELS');
const feed = DATA.posts.filter(p => p.type === 'FEED');
function avg(arr, key) {{ const vals = arr.map(p => p[key]).filter(v => v != null); return vals.length ? Math.round(vals.reduce((s,v)=>s+v,0)/vals.length) : 0; }}
new Chart(document.getElementById('typeChart'), {{
  type: 'bar',
  data: {{
    labels: ['도달', '조회수', '좋아요', '댓글', '공유', '저장'],
    datasets: [
      {{ label: `REELS (${{reels.length}}개 평균)`, data: ['reach','views','likes','comments','shares','saved'].map(k => avg(reels,k)), backgroundColor: '#7F77DD' }},
      {{ label: `FEED (${{feed.length}}개 평균)`, data: ['reach','views','likes','comments','shares','saved'].map(k => avg(feed,k)), backgroundColor: '#1D9E75' }}
    ]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ position: 'top', labels: {{ font: {{size: 12}}, boxWidth: 12 }} }} }}, scales: {{ y: {{ ticks: {{ font:{{size:11}} }} }}, x: {{ ticks: {{ font:{{size:11}} }} }} }} }}
}});

// Age
new Chart(document.getElementById('ageChart'), {{
  type: 'bar',
  data: {{ labels: Object.keys(DATA.age), datasets: [{{ data: Object.values(DATA.age), backgroundColor: '#534AB7' }}] }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
}});

// Gender
new Chart(document.getElementById('genderChart'), {{
  type: 'doughnut',
  data: {{ labels: Object.keys(DATA.gender), datasets: [{{ data: Object.values(DATA.gender), backgroundColor: ['#D4537E','#378ADD','#888780'] }}] }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ position: 'right', labels: {{ font: {{size: 12}}, boxWidth: 12 }} }} }} }}
}});

// Cities
new Chart(document.getElementById('cityChart'), {{
  type: 'bar',
  data: {{ labels: DATA.cities.map(c => c[0]), datasets: [{{ data: DATA.cities.map(c => c[1]), backgroundColor: '#0F6E56' }}] }},
  options: {{ indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
}});

// Table
let sortKey = 'ts', sortDir = -1;
function renderTable() {{
  const sorted = [...DATA.posts].sort((a,b) => {{
    const av = a[sortKey], bv = b[sortKey];
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === 'string') return av < bv ? -sortDir : av > bv ? sortDir : 0;
    return (av - bv) * sortDir;
  }});
  const cols = [
    {{k:'ts', label:'날짜'}}, {{k:'type', label:'타입'}}, {{k:'cap', label:'캡션'}},
    {{k:'reach', label:'도달', n:true}}, {{k:'views', label:'조회', n:true}},
    {{k:'likes', label:'좋아', n:true}}, {{k:'comments', label:'댓글', n:true}},
    {{k:'shares', label:'공유', n:true}}, {{k:'saved', label:'저장', n:true}},
    {{k:'ti', label:'참여', n:true}}, {{k:'pv', label:'프로필', n:true}}, {{k:'fl', label:'팔로우', n:true}}
  ];
  let html = '<table><thead><tr>';
  cols.forEach(c => {{
    const arrow = sortKey === c.k ? (sortDir === 1 ? ' ↑' : ' ↓') : '';
    html += `<th data-k="${{c.k}}" style="text-align:${{c.n?'right':'left'}};">${{c.label}}${{arrow}}</th>`;
  }});
  html += '</tr></thead><tbody>';
  sorted.forEach(p => {{
    const pillClass = p.type === 'REELS' ? 'pill-reels' : 'pill-feed';
    html += `<tr data-pl="${{p.pl}}">`;
    html += `<td>${{p.ts.substring(5)}}</td>`;
    html += `<td><span class="pill ${{pillClass}}">${{p.type}}</span></td>`;
    html += `<td style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${{(p.cap||'').replace(/</g,'&lt;')}}</td>`;
    ['reach','views','likes','comments','shares','saved','ti','pv','fl'].forEach(k => {{
      const v = p[k];
      const disp = v == null ? '<span style="color:var(--text-3);">-</span>' : v.toLocaleString();
      html += `<td class="num">${{disp}}</td>`;
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
    const pl = tr.getAttribute('data-pl');
    if (pl) window.open(pl, '_blank');
  }}));
}}
renderTable();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    TOKEN = os.environ.get("BISHOKU_MONO_API_KEY")
    if not TOKEN:
        print("ERROR: BISHOKU_MONO_API_KEY env var not set", file=sys.stderr)
        sys.exit(1)

    # Optional: refresh token first (extends expiry)
    new_token = refresh_token()
    if new_token:
        TOKEN = new_token

    snap = fetch_all()
    history = append_history(snap)
    render_html(snap, history)

    # Write refreshed token to GITHUB_OUTPUT if available (so workflow can update secret)
    if new_token and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"new_token={new_token}\n")
        print("[token] wrote refreshed token to GITHUB_OUTPUT (rotate secret if changed)")

    print("[done]")
