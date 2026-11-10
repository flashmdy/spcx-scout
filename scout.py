#!/usr/bin/env python3
"""SPCX 출처 탐색 수집기 (모델 없음, 표준 라이브러리만).
하는 일: 승인된 피드·API에서 SpaceX 글을 읽고, 글이 인용한 바깥 사이트를 세어 새 출처 후보를 찾고,
기계 시험(robots 허용·피드 유무·게시일·최근 7일 글 수·키워드 적중)을 한 뒤 data/candidates.json 에 쓴다.
규칙: config.json 의 blocked 사이트에는 절대 접속하지 않는다. robots.txt 가 거부하면 접속하지 않는다.
사용: python3 scout.py            (실행)
      python3 scout.py --selftest (네트워크 없이 규칙 시험)"""
import json, os, re, sys, time, datetime as dt
import urllib.request, urllib.parse, urllib.robotparser, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
UA = CFG["user_agent"]
NOW = dt.datetime.now(dt.timezone.utc)
ARTICLES = []             # 이번 실행에서 읽은 SpaceX 글(정리 시스템 입력)
SEED_DOMAINS = set()      # 씨앗 출처 자기 도메인(후보에서 제외) — collect()에서 채움
STATS = {"requests": 0, "blocked_skipped": 0, "robots_denied": 0, "errors": 0, "blocked_contacted": 0}
_ROBOTS, _LAST, _ROBOTS_ERR = {}, {}, set()


def host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def in_list(host, names):
    return next((n for n in names if host == n or host.endswith("." + n)), None)


def base_domain(host):
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac", "go", "gov", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _raw_get(url, limit=1_500_000, timeout=15):
    """실제 네트워크 접속은 이 함수 하나뿐이다(시험에서 바꿔 끼운다)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode(r.headers.get_content_charset() or "utf-8", "replace")


def robots_ok(url):
    p = urllib.parse.urlsplit(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        try:
            STATS["requests"] += 1
            rp.parse(_raw_get(root + "/robots.txt", limit=300_000).splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.parse(["User-agent: *", "Disallow: /"])   # 접근 거부 = 거부로 본다
            else:
                rp.parse([])                                   # 404 등 = 규칙 없음
        except Exception:
            rp.parse(["User-agent: *", "Disallow: /"])       # 확인 못 하면 접속하지 않는다
            _ROBOTS_ERR.add(root)
        _ROBOTS[root] = rp
    return _ROBOTS[root].can_fetch(UA, url)


def fetch(url):
    """차단 목록 → robots → 접속 순서. 반환 (본문 또는 None, 사유)."""
    h = host_of(url)
    if not h or not url.startswith("http"):
        return None, "주소 오류"
    if in_list(h, CFG["blocked"]):
        STATS["blocked_skipped"] += 1
        return None, "차단 목록"
    if not robots_ok(url):
        STATS["robots_denied"] += 1
        p = urllib.parse.urlsplit(url)
        return None, "robots 확인 실패" if f"{p.scheme}://{p.netloc}" in _ROBOTS_ERR else "robots 거부"
    wait = 1.0 - (time.time() - _LAST.get(h, 0))
    if wait > 0:
        time.sleep(wait)
    try:
        STATS["requests"] += 1
        if in_list(h, CFG["blocked"]):            # 이중 확인: 여기 걸리면 버그
            STATS["blocked_contacted"] += 1
            return None, "차단 목록"
        body = _raw_get(url)
        _LAST[h] = time.time()
        return body, "성공"
    except Exception as e:
        STATS["errors"] += 1
        _LAST[h] = time.time()
        return None, f"오류: {type(e).__name__}"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs, self.feeds = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href", "").startswith("http"):
            self.hrefs.append(a["href"])
        if tag == "link" and "alternate" in (a.get("rel") or "") and re.search(r"rss|atom", a.get("type") or ""):
            self.feeds.append(a.get("href") or "")


def links_in(html):
    p = Links()
    try:
        p.feed(html or "")
    except Exception:
        pass
    return p


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def parse_feed(xml):
    """RSS·Atom → [{title, link, date, html}]"""
    items = []
    try:
        root = ET.fromstring(xml.encode("utf-8", "replace"))
    except ET.ParseError:
        return items
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "date": None, "html": ""}
        for c in el:
            t = c.tag.split("}")[-1]
            if t == "title":
                d["title"] = (c.text or "").strip()
            elif t == "link":
                d["link"] = d["link"] or (c.text or "").strip() or c.attrib.get("href", "")
            elif t in ("pubDate", "published", "updated", "date"):
                d["date"] = d["date"] or parse_date(c.text)
            elif t in ("description", "encoded", "content", "summary"):
                d["html"] += " " + (c.text or "")
        items.append(d)
    return items


def has_kw(text):
    t = (text or "").lower()
    return any(k in t for k in CFG["keywords"])


def legal_path(url):
    path = urllib.parse.urlsplit(url).path.lower()
    return any(w in path for w in CFG.get("skip_path_words", []))


def to_dt(v):
    """여러 날짜 형식 → UTC datetime (GDELT 20261110T041500Z, ISO, RFC822, 2026-11-09)."""
    if isinstance(v, dt.datetime):
        return v
    v = (v or "").strip()
    for f in ("%Y%m%dT%H%M%SZ", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(v, f).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return parse_date(v)


def keep(url, title, when, seed):
    """정리 시스템에 넣을 글 1건. 차단·무시 사이트 글은 넣지 않는다(접속도 안 함)."""
    h = host_of(url)
    if not h or not title or in_list(h, CFG["blocked"]) or in_list(h, CFG["ignore"]) or legal_path(url):
        return
    d = to_dt(when)
    if d and (NOW - d).days > 7:                      # 피드에 섞인 옛날 글은 버린다
        return
    ARTICLES.append({"url": url, "title": " ".join(title.split())[:200], "domain": base_domain(h),
                     "published": d.isoformat(timespec="minutes") if d else "", "seed": seed})


def collect(fail_streak=None):
    """씨앗 출처에서 SpaceX 글을 읽고 인용된 바깥 사이트를 센다.
    5회 연속 실패한 씨앗은 하루 1번(UTC 0시대 실행)만 다시 시도한다."""
    cites, seed_log, blocked_cited = {}, [], {}
    fail_streak = fail_streak or {}
    SEED_DOMAINS.update(base_domain(host_of(s["url"])) for s in CFG["seeds"] if s["kind"] == "rss")

    def add(url, seed, title):
        h = host_of(url)
        if not h:
            return
        b = base_domain(h)
        if in_list(h, CFG["blocked"]):
            blocked_cited[b] = blocked_cited.get(b, 0) + 1
            return
        if in_list(h, CFG["known"]) or in_list(h, CFG["ignore"]) or b in SEED_DOMAINS:
            return
        if legal_path(url):                           # 개인정보·약관·구독 등 법적·안내 페이지 제외
            return
        c = cites.setdefault(b, {"count": 0, "seeds": set(), "sample": url, "titles": []})
        c["count"] += 1
        c["seeds"].add(seed)
        if len(c["titles"]) < 2 and title:
            c["titles"].append(title[:90])

    for s in CFG["seeds"]:
        if fail_streak.get(s["name"], 0) >= 5 and NOW.hour >= 6:
            seed_log.append({"name": s["name"], "result": "일시 중지(5회 연속 실패)", "items": 0, "spacex_items": 0})
            continue
        body, why = fetch(s["url"])
        row = {"name": s["name"], "result": why, "items": 0, "spacex_items": 0}
        if body and s["kind"] in ("hn", "fedreg", "gdelt"):
            try:
                js = json.loads(body)
            except ValueError:
                row["result"], body = "형식 오류(JSON 아님)", None
        if body:
            if s["kind"] == "gdelt":
                arts = js.get("articles", [])
                row["items"] = len(arts)
                for a in arts:
                    if a.get("url") and has_kw(a.get("title")):
                        row["spacex_items"] += 1
                        add(a["url"], s["name"], a.get("title"))
                        keep(a["url"], a.get("title"), a.get("seendate"), s["name"])
            elif s["kind"] == "hn":
                hits = js.get("hits", [])
                row["items"] = len(hits)
                for h in hits:
                    if h.get("url") and has_kw(h.get("title")):
                        row["spacex_items"] += 1
                        add(h["url"], s["name"], h.get("title"))
                        keep(h["url"], h.get("title"), h.get("created_at"), s["name"])
            elif s["kind"] == "fedreg":
                res = js.get("results", [])
                row["items"] = row["spacex_items"] = len(res)
                for r in res:
                    keep(r.get("html_url", ""), r.get("title"), r.get("publication_date"), s["name"])
            else:
                items = parse_feed(body)
                row["items"] = len(items)
                own = base_domain(host_of(s["url"]))
                pages = 0
                for it in items:
                    if not has_kw(it["title"] + " " + it["html"]):
                        continue
                    row["spacex_items"] += 1
                    keep(it["link"], it["title"], it["date"], s["name"])
                    html = it["html"]
                    if pages < CFG["article_pages_per_seed"] and it["link"]:
                        page, _ = fetch(it["link"])
                        pages += 1
                        html += " " + (page or "")
                    per_article = {}                  # 한 글 안에서 같은 사이트는 1번만 센다(머리글·바닥글 링크 부풀림 방지)
                    for u in links_in(html).hrefs:
                        b = base_domain(host_of(u))
                        if b and b != own and not legal_path(u):
                            per_article.setdefault(b, u)
                    for u in per_article.values():
                        add(u, s["name"], it["title"])
        seed_log.append(row)
    return cites, seed_log, blocked_cited


def test_candidate(domain, info):
    """기계 시험: robots 허용 → 첫 화면 → 피드 → 게시일·최근 7일·키워드."""
    out = {"domain": domain, "citations": info["count"], "cited_by": sorted(info["seeds"]), "sample": info["sample"],
           "titles": info["titles"], "robots": "", "feed": "", "has_dates": False, "items_7d": 0, "kw_ratio": 0.0,
           "verdict": "", "score": 0.0}
    home = f"https://{domain}/"
    page, why = fetch(home)
    out["robots"] = "허용" if why == "성공" else why
    if not page:
        out["verdict"] = "⛔ 접속 불가" if why in ("robots 거부", "차단 목록") else "부분(첫 화면 실패)"
        return out
    feeds = [urllib.parse.urljoin(home, f) for f in links_in(page).feeds if f][:1] or [home + "feed/"]
    xml, why2 = fetch(feeds[0])
    items = parse_feed(xml) if xml else []
    if not items:
        out["verdict"] = "부분(피드 없음)"
        out["score"] = round(info["count"] * 0.1, 2)
        return out
    out["feed"] = feeds[0]
    dated = [i for i in items if i["date"]]
    out["has_dates"] = bool(dated)
    out["items_7d"] = sum(1 for i in dated if (NOW - i["date"]).days <= 7)
    out["kw_ratio"] = round(sum(1 for i in items if has_kw(i["title"] + " " + i["html"])) / len(items), 2)
    fresh = out["has_dates"] and out["items_7d"] > 0
    if fresh and out["kw_ratio"] > 0:
        out["verdict"] = "기계 시험 통과(본문·신뢰도는 봇이 확인)"
    elif fresh:
        out["verdict"] = "부분(SpaceX 글 없음)"      # 키워드 적중 0이면 SpaceX 출처가 아님
    else:
        out["verdict"] = "부분(날짜·최신성 미달)"
    out["score"] = round(info["count"] * (1 + out["items_7d"]) * (0.2 + out["kw_ratio"]), 2)
    return out


def load(name, default):
    try:
        return json.load(open(os.path.join(DATA, name), encoding="utf-8"))
    except Exception:
        return default


def main():
    os.makedirs(DATA, exist_ok=True)
    state = load("state.json", {"cites": {}, "fail_streak": {}})
    for dom in [d for d, st in state["cites"].items() if legal_path(st.get("sample", ""))]:
        del state["cites"][dom]                       # 예전 실행이 남긴 약관·개인정보 링크 후보 정리
    cites, seed_log, blocked_cited = collect(state["fail_streak"])
    today = NOW.date().isoformat()
    cutoff = (NOW - dt.timedelta(days=CFG["window_days"])).date().isoformat()
    for dom, c in cites.items():                      # 30일 누적(날짜별)
        st = state["cites"].setdefault(dom, {"days": {}, "seeds": [], "sample": c["sample"], "titles": []})
        st["days"][today] = max(st["days"].get(today, 0), c["count"])
        st["seeds"] = sorted(set(st["seeds"]) | c["seeds"])
        st["titles"] = (c["titles"] + st["titles"])[:2]
    for dom in list(state["cites"]):
        d = {k: v for k, v in state["cites"][dom]["days"].items() if k >= cutoff}
        if d:
            state["cites"][dom]["days"] = d
        else:
            del state["cites"][dom]
    for r in seed_log:                                 # 연속 실패 수
        n = r["name"]
        if r["result"].startswith("일시 중지"):
            continue
        state["fail_streak"][n] = 0 if r["result"] == "성공" else state["fail_streak"].get(n, 0) + 1
    ranked = sorted(state["cites"].items(), key=lambda kv: (-sum(kv[1]["days"].values()), kv[0]))
    tested = []
    for dom, st in ranked[:CFG["max_candidates_tested"]]:
        tested.append(test_candidate(dom, {"count": sum(st["days"].values()), "seeds": set(st["seeds"]),
                                           "sample": st["sample"], "titles": st["titles"]}))
    tested.sort(key=lambda x: -x["score"])
    top = tested[:CFG["top_n"]]
    passed = [t for t in tested if t["verdict"].startswith("기계 시험 통과")]
    failing = sorted(n for n, k in state["fail_streak"].items() if k >= 3)
    workers = 0 if not passed else (2 if len(passed) >= 6 else 1)
    if failing:
        workers = max(workers, 1)
    out = {"generated_at": NOW.isoformat(timespec="seconds"),
           "signals": {"candidates_passed": len(passed), "candidates_seen_30d": len(state["cites"]),
                       "feeds_failing_3x": failing, "suggested_workers": workers},
           "top": top, "seeds": seed_log,
           "blocked_cited": dict(sorted(blocked_cited.items(), key=lambda kv: -kv[1])[:10]),
           "stats": STATS}
    json.dump(out, open(os.path.join(DATA, "candidates.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(state, open(os.path.join(DATA, "state.json"), "w", encoding="utf-8"), ensure_ascii=False)
    log = os.path.join(DATA, "log.csv")
    new = not os.path.exists(log)
    with open(log, "a", encoding="utf-8") as f:
        if new:
            f.write("time_utc,seeds_ok,seeds_total,requests,blocked_skipped,robots_denied,errors,blocked_contacted,passed,workers\n")
        ok = sum(1 for r in seed_log if r["result"] == "성공")
        f.write(f"{out['generated_at']},{ok},{len(seed_log)},{STATS['requests']},{STATS['blocked_skipped']},"
                f"{STATS['robots_denied']},{STATS['errors']},{STATS['blocked_contacted']},{len(passed)},{workers}\n")
    out["news"] = update_news(ARTICLES)
    write_report(out, log)
    print(json.dumps({"signals": out["signals"], "stats": STATS,
                      "seeds": [(r["name"], r["result"], r["spacex_items"]) for r in seed_log]}, ensure_ascii=False))
    if STATS["blocked_contacted"]:
        sys.exit(2)                                    # 차단 사이트 접속은 실패로 처리


def norm_url(u):
    p = urllib.parse.urlsplit(u)
    q = "&".join(x for x in p.query.split("&") if x and not x.startswith(("utm_", "fbclid", "gclid", "ref=", "cmpid")))
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower())[:80]


def topics_of(title):
    t = title.lower()
    hit = [name for name, words in CFG.get("topics", {}).items() if any(w in t for w in words)]
    return hit or ["기타"]


def update_news(new_items):
    """글 정리: 중복 합치기(같은 주소·같은 제목) → 주제 분류 → 교차 확인 수 → 저장·요약 화면.
    파일: data/articles.json(최근 30일), data/archive/YYYY-MM.jsonl(영구), data/news/날짜.md(하루치),
          data/NEWS.md(최근 48시간 화면), data/news_latest.json(봇이 읽을 작은 파일)."""
    store = load("articles.json", {})
    by_url = {a["key_url"]: k for k, a in store.items()}
    by_title = {a["key_title"]: k for k, a in store.items()}
    official = set(CFG.get("official_seeds", []))
    now_s = NOW.isoformat(timespec="minutes")
    fresh = []
    for it in new_items:
        ku, kt = norm_url(it["url"]), norm_title(it["title"])
        k = by_url.get(ku) or (by_title.get(kt) if len(kt) >= 25 else None)
        if k:                                          # 이미 있는 글 → 출처만 합친다
            a = store[k]
            if it["domain"] not in a["domains"]:
                a["domains"].append(it["domain"])
            if it["seed"] not in a["seeds"]:
                a["seeds"].append(it["seed"])
            a["published"] = a["published"] or it["published"]
            continue
        k = re.sub(r"[^a-z0-9]", "", ku)[-60:] or kt
        a = {"title": it["title"], "url": it["url"], "domains": [it["domain"]], "seeds": [it["seed"]],
             "published": it["published"], "first_seen": now_s, "topics": topics_of(it["title"]),
             "key_url": ku, "key_title": kt}
        store[k] = a
        by_url[ku], by_title[kt] = k, k
        fresh.append(a)
    cutoff = (NOW - dt.timedelta(days=CFG["window_days"])).isoformat()
    store = {k: a for k, a in store.items() if (a["published"] or a["first_seen"]) >= cutoff}
    for a in store.values():
        a["official"] = any(sd in official for sd in a["seeds"])
        a["score"] = len(a["domains"]) + (2 if a["official"] else 0)
    json.dump(store, open(os.path.join(DATA, "articles.json"), "w", encoding="utf-8"), ensure_ascii=False)
    if fresh:                                          # 영구 보관(월별, 새 글만 덧붙임)
        os.makedirs(os.path.join(DATA, "archive"), exist_ok=True)
        with open(os.path.join(DATA, "archive", NOW.strftime("%Y-%m") + ".jsonl"), "a", encoding="utf-8") as f:
            for a in fresh:
                f.write(json.dumps({x: a[x] for x in ("title", "url", "domains", "seeds", "published", "first_seen", "topics")},
                                   ensure_ascii=False) + "\n")

    def when(a):
        return a["published"] or a["first_seen"]

    def ts(iso):                                       # 최신 글이 위로 오도록 음수 시각
        try:
            return -dt.datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return 0

    def kst(iso):
        try:
            return (dt.datetime.fromisoformat(iso) + dt.timedelta(hours=9)).strftime("%m-%d %H:%M")
        except ValueError:
            return "-"

    def block(items, title, per_topic):
        L = [f"# {title}", "", f"갱신: {(NOW + dt.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')} (한국 시간) · 글 {len(items)}건 · "
             f"교차 확인 = 같은 소식을 낸 사이트 수 · 🏛 = 정부·공식 출처", ""]
        order = list(CFG.get("topics", {}).keys()) + ["기타"]
        for tp in order:
            grp = sorted([a for a in items if a["topics"][0] == tp], key=lambda a: (-a["score"], ts(when(a))))
            if not grp:
                continue
            L += [f"## {tp} ({len(grp)})", ""]
            for a in grp[:per_topic]:
                badge = "🏛 " if a["official"] else ""
                cross = f" · 교차 {len(a['domains'])}곳" if len(a["domains"]) > 1 else ""
                L.append(f"- {badge}[{a['title']}]({a['url']}) — {a['domains'][0]} · {kst(when(a))}{cross}")
            if len(grp) > per_topic:
                L.append(f"- …외 {len(grp) - per_topic}건")
            L.append("")
        L.append("※ 제목·링크 모음입니다. 내용 확인과 신뢰도 판정은 봇·사람이 따로 합니다. 정보 제공용이며 투자 자문이 아닙니다.")
        return "\n".join(L) + "\n"

    h48 = (NOW - dt.timedelta(hours=48)).isoformat()
    recent = [a for a in store.values() if when(a) >= h48]
    open(os.path.join(DATA, "NEWS.md"), "w", encoding="utf-8").write(block(recent, "SPCX 뉴스 모음 — 최근 48시간", 12))
    day = (NOW + dt.timedelta(hours=9)).date()
    d0 = (dt.datetime.combine(day, dt.time(), dt.timezone(dt.timedelta(hours=9)))).astimezone(dt.timezone.utc).isoformat()
    today = [a for a in store.values() if a["first_seen"] >= d0 or when(a) >= d0]
    os.makedirs(os.path.join(DATA, "news"), exist_ok=True)
    open(os.path.join(DATA, "news", f"{day.isoformat()}.md"), "w", encoding="utf-8").write(
        block(today, f"SPCX 뉴스 — {day.isoformat()} (한국 날짜)", 30))
    top = sorted(recent, key=lambda a: (-a["score"], ts(when(a))))[:40]
    json.dump({"generated_at": NOW.isoformat(timespec="seconds"), "count_48h": len(recent), "new_this_run": len(fresh),
               "items": [{x: a[x] for x in ("title", "url", "domains", "published", "topics", "official", "score")} for a in top]},
              open(os.path.join(DATA, "news_latest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"new": len(fresh), "h48": len(recent), "stored": len(store)}


def write_report(out, log_path):
    """사람이 읽는 요약 data/REPORT.md (GitHub 화면에서 표로 보임)."""
    sg, st = out["signals"], out["stats"]
    ok = [r for r in out["seeds"] if r["result"] == "성공"]
    bad = [r for r in out["seeds"] if r["result"] != "성공"]
    kst = (NOW + dt.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    L = [f"# SPCX 출처 탐색 결과", "",
         f"마지막 실행: **{kst} (한국 시간)** · 6시간마다 자동 갱신", "",
         "| 항목 | 값 |", "|---|---|",
         f"| 차단 사이트 접속 | {'✅ 0건' if not st['blocked_contacted'] else '⛔ ' + str(st['blocked_contacted']) + '건'} |",
         f"| 씨앗 출처 성공 | {len(ok)}/{len(out['seeds'])} |",
         f"| 기계 시험 통과 후보 | {sg['candidates_passed']} |",
         f"| 최근 30일 본 후보 | {sg['candidates_seen_30d']} |",
         f"| 봇이 부를 일꾼 수 | {sg['suggested_workers']} |",
         f"| 3회 연속 실패 씨앗 | {', '.join(sg['feeds_failing_3x']) or '없음'} |",
         f"| 뉴스 글(이번 새 글 / 48시간 / 보관 30일) | {out.get('news', {}).get('new', 0)} / {out.get('news', {}).get('h48', 0)} / {out.get('news', {}).get('stored', 0)} |", "",
         "📰 정리된 뉴스: [NEWS.md](NEWS.md) · 날짜별: [news 폴더](news/)", "",
         "## 새 출처 후보 (점수순)", "",
         "| # | 사이트 | 판정 | 인용 | 7일 글 | SpaceX 비율 | 피드 |", "|---|---|---|---|---|---|---|"]
    for i, t in enumerate(out["top"], 1):
        mark = "✅" if t["verdict"].startswith("기계 시험 통과") else ("⛔" if t["verdict"].startswith("⛔") else "△")
        feed = f"[피드]({t['feed']})" if t["feed"] else "-"
        L.append(f"| {i} | {t['domain']} | {mark} {t['verdict']} | {t['citations']} | {t['items_7d']} | {int(t['kw_ratio']*100)}% | {feed} |")
    L += ["", "✅ 통과(본문·신뢰도는 봇이 따로 확인) · △ 부분 · ⛔ 접속 불가", ""]
    if bad:
        L += ["## 실패한 씨앗 출처", "", "| 출처 | 사유 |", "|---|---|"]
        L += [f"| {r['name']} | {r['result']} |" for r in bad]
        L.append("")
    L += ["## 씨앗 출처별 SpaceX 글 수", "", "| 출처 | 전체 | SpaceX |", "|---|---|---|"]
    L += [f"| {r['name']} | {r['items']} | {r['spacex_items']} |" for r in sorted(ok, key=lambda r: -r["spacex_items"])]
    if out["blocked_cited"]:
        L += ["", "## 차단 사이트 인용 횟수 (접속 안 함)", "",
              ", ".join(f"{d} {n}" for d, n in out["blocked_cited"].items())]
    try:
        rows = open(log_path, encoding="utf-8").read().strip().splitlines()[1:][-10:]
        L += ["", "## 최근 실행 10회", "", "| 시각(UTC) | 씨앗 성공 | 요청 | 오류 | 차단 접속 | 통과 |", "|---|---|---|---|---|---|"]
        for r in reversed(rows):
            c = r.split(",")
            L.append(f"| {c[0][:16].replace('T', ' ')} | {c[1]}/{c[2]} | {c[3]} | {c[6]} | {c[7]} | {c[8]} |")
    except Exception:
        pass
    L += ["", "※ 기계 시험은 접속·형식만 본 것입니다. 정보 제공용이며 투자 자문이 아닙니다."]
    open(os.path.join(DATA, "REPORT.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")


def selftest():
    """네트워크 없이: 차단 사이트·robots 거부 사이트에 한 번도 접속하지 않는지 확인."""
    global _raw_get
    contacted = []
    real_seeds = [s for s in CFG["seeds"]]
    seeds_blocked = [s["name"] for s in real_seeds if in_list(host_of(s["url"]), CFG["blocked"])]
    seed_names_unique = len({s["name"] for s in real_seeds}) == len(real_seeds)
    blocked_host = CFG["blocked"][0]
    feed = ('<rss><channel><item><title>SpaceX Starlink test launch from Florida</title><link>https://seed.example/a1</link>'
            '<pubDate>' + NOW.strftime("%a, %d %b %Y %H:%M:%S +0000") + '</pubDate><description><![CDATA['
            f'<a href="https://www.{blocked_host}/x/1">b</a> <a href="https://good.example/p/1">g</a> '
            '<a href="https://deny.example/p/2">d</a> <a href="https://good.example/p/3">g2</a>]]></description></item></channel></rss>')
    pages = {
        "https://seed.example/robots.txt": "User-agent: *\nAllow: /",
        "https://seed.example/feed": feed,
        "https://seed.example/a1": f'<a href="https://{blocked_host}/y">again</a>',
        "https://good.example/robots.txt": "User-agent: *\nAllow: /",
        "https://good.example/": '<link rel="alternate" type="application/rss+xml" href="/rss.xml">',
        "https://good.example/rss.xml": feed.replace("seed.example", "good.example"),
        "https://deny.example/robots.txt": "User-agent: *\nDisallow: /",
    }

    def fake(url, limit=0, timeout=0):
        contacted.append(url)
        if url in pages:
            return pages[url]
        raise urllib.error.HTTPError(url, 404, "nf", None, None)
    _raw_get = fake
    pages["https://api.example/robots.txt"] = ""
    pages["https://api.example/hn"] = json.dumps({"hits": [{"title": "SpaceX news", "url": "https://good.example/p/9"}, {"title": "Other", "url": "https://other.example/z"}]})
    pages["https://api.example/fr"] = json.dumps({"count": 1, "results": [{"title": "FCC rule"}]})
    pages["https://api.example/gd"] = json.dumps({"articles": [
        {"url": "https://gd.example/n/1", "title": "SpaceX Starship flight"},
        {"url": f"https://www.{blocked_host}/n/2", "title": "SpaceX blocked outlet"},
        {"url": "https://gd2.example/privacy", "title": "SpaceX privacy"},
        {"url": "https://dup.example/n/7", "title": "SpaceX Starlink test launch from Florida"}]})
    pages["https://api.example/bad"] = "<html>rate limited</html>"
    CFG["seeds"] = [{"name": "seed", "kind": "rss", "url": "https://seed.example/feed"},
                    {"name": "hn", "kind": "hn", "url": "https://api.example/hn"},
                    {"name": "fr", "kind": "fedreg", "url": "https://api.example/fr"},
                    {"name": "gd", "kind": "gdelt", "url": "https://api.example/gd"},
                    {"name": "bad", "kind": "gdelt", "url": "https://api.example/bad"}]
    cites, seed_log, blocked_cited = collect()
    res = {d: test_candidate(d, c) for d, c in cites.items()}
    import tempfile
    globals()["DATA"] = tempfile.mkdtemp()
    news = update_news(ARTICLES)
    store = json.load(open(os.path.join(DATA, "articles.json"), encoding="utf-8"))
    merged = [a for a in store.values() if a["key_title"].startswith("spacexstarlinktestlaunch")]
    news_md = open(os.path.join(DATA, "NEWS.md"), encoding="utf-8").read()
    checks = {
        "차단 사이트 접속 0건": not any(in_list(host_of(u), CFG["blocked"]) for u in contacted),
        "차단 사이트는 인용 수만 기록": sum(blocked_cited.values()) == 2,   # 같은 글 안 중복은 1번(글 1 + GDELT 1)
        "robots 거부 사이트는 robots.txt 외 접속 0건": [u for u in contacted if "deny.example" in u] == ["https://deny.example/robots.txt"],
        "robots 거부 판정 ⛔": res["deny.example"]["verdict"].startswith("⛔"),
        "정상 후보 통과": res["good.example"]["verdict"].startswith("기계 시험 통과") and res["good.example"]["citations"] == 2,   # 같은 글 안 2링크=1 + HN 1
        "씨앗 자기 사이트는 후보 아님": "seed.example" not in cites,
        "blocked_contacted 0": STATS["blocked_contacted"] == 0,
        "HN은 키워드 글만 세고 정부 API도 읽음": "other.example" not in cites and [r["spacex_items"] for r in seed_log][:3] == [1, 1, 1],
        "GDELT 읽기(차단·약관 링크 제외)": "gd.example" in cites and "gd2.example" not in cites,
        "JSON 아닌 응답은 형식 오류로 기록": seed_log[4]["result"].startswith("형식 오류"),
        "뉴스: 차단·약관 글 제외": not any(blocked_host in a["url"] or "privacy" in a["url"] for a in store.values()),
        "뉴스: 같은 제목은 1건으로 합치고 교차 2곳": len(merged) == 1 and len(merged[0]["domains"]) == 2,
        "뉴스: 주제 분류·화면 생성": all(a["topics"] for a in store.values()) and "SPCX 뉴스 모음" in news_md and news["stored"] == len(store),
        "실제 설정: 씨앗 중 차단 사이트 0개": not seeds_blocked,
        "실제 설정: 씨앗 이름 중복 없음": seed_names_unique,
    }
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    print(f"자체 시험 {sum(checks.values())}/{len(checks)}")
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
