"""
Reddit (r/GamingLeaksAndRumours) の新着を取得し、
Gemini（無料枠）または Claude で日本語に要約して Discord に通知、(有効時は) X にも投稿するスクリプト。
GitHub Actions で定期実行する想定。
"""
import calendar
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

# ===== 設定 =====
SUBREDDIT = "GamingLeaksAndRumours"
# 取得元: "arctic"（Arctic Shift の JSON API。フレアで絞り込める）または "rss"（Reddit 本体の RSS）
# arctic が取れないときは自動で rss に切り替える
SOURCE = (os.environ.get("SOURCE") or "arctic").lower()
ARCTIC_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCTIC_LIMIT = 100
# 通知するフレア（カンマ区切り。空なら全部）。例: "Leak" / "Leak,Rumour"
FLAIR_FILTER = [f.strip() for f in (os.environ.get("FLAIR_FILTER") or "Leak").split(",") if f.strip()]
FEED_URL = os.environ.get("FEED_URL") or "https://www.reddit.com/r/GamingLeaksAndRumours/new/.rss"
# rss で取るときにフレアで絞る場合は Reddit 検索の RSS を使う
FLAIR_FEED_URL = ("https://www.reddit.com/r/GamingLeaksAndRumours/search.rss?restrict_sr=1&sort=new&q="
                  + "+OR+".join(requests.utils.quote(f'flair:"{f}"') for f in FLAIR_FILTER))
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TRANSLATOR = (os.environ.get("TRANSLATOR") or "gemini").lower()  # "gemini"（無料）または "claude"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
# メインのモデルが混雑・制限中のときに順番に試す予備モデル（カンマ区切り）
# 無料枠は Flash 系が1日20回ずつ、Lite が1日500回。上から順に使い、上限に来たら次へ回す
GEMINI_FALLBACK_MODELS = (os.environ.get("GEMINI_FALLBACK_MODELS")
                          or "gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite")
GEMINI_THINKING = os.environ.get("GEMINI_THINKING") or "low"  # 考える量を減らして応答を速くする
TITLE_DEDUP_DAYS = 3      # 同じタイトルの投稿がこの日数以内に来たら送らない
MAX_TITLES = 500          # 覚えておくタイトルの数
MAX_TRANSLATE_TRIES = 3   # 翻訳に失敗したら、この回数の実行まで持ち越して再挑戦する
FETCH_ALERT_AFTER = 8     # Reddit の取得失敗がこの回数（15分おきなら約2時間）続いたらエラーで知らせる
SWITCH_MARGIN = 60 * 60  # 取得元の切替時、前回実行のこの秒数前より新しい投稿は通知候補に残す
RUN_TIME_BUDGET = 6 * 60  # 1回の実行で翻訳に使う最大秒数（残りは次回へ）
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL") or "claude-haiku-4-5-20251001"
ENABLE_X = (os.environ.get("ENABLE_X") or "false").lower() == "true"
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN") or "20")  # 実際は持ち時間(6分)で打ち切り
SOURCE_LABEL = "r/GamingLeaksAndRumours"

STATE_FILE = Path(__file__).with_name("seen.json")
MAX_SEEN = 500  # 既読IDの保存上限
USER_AGENT = "python:jp-leak-notifier:v1.0 (personal use)"

PROMPT = """以下は Reddit の r/GamingLeaksAndRumours に投稿された、ゲームのリーク・噂に関する投稿です。
日本のゲームファン向けに、内容を日本語で要約してください。

# ルール
- ゲームタイトル・会社名は、日本での公式表記が確実にわかる場合はそれを使い、わからなければ英語のまま書く
- リーク・噂・未確認の情報は断定しない（「〜との情報」「〜とされる」など）
- 情報源（dataminer、insider、公式発表など）が読み取れる場合は要約に含める
- 本文にない情報を推測で足さない

# 出力形式（JSONのみ。前置きやコードブロックは不要）
{{"title_ja": "40字以内の日本語タイトル",
  "summary_ja": "200字以内の日本語要約（Discord用）",
  "tweet": "110字以内の投稿文（X用。URL・ハッシュタグ・出典は書かない）"}}

# 投稿タイトル
{title}

# 投稿本文（先頭のみ）
{body}
"""


# ===== 既読管理 =====
def load_state():
    """seen.json を読む。旧形式（IDのリスト）にも対応。初回は None。"""
    if not STATE_FILE.exists():
        return None
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"seen": data}
    data.setdefault("fails", {})
    data.setdefault("titles", {})
    return data


def save_state(state):
    del state["seen"][:-MAX_SEEN]  # 同じリストのまま古い分を削る（参照を切らない）
    titles = state.get("titles", {})
    for k in sorted(titles, key=titles.get)[:-MAX_TITLES]:
        titles.pop(k)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ===== Reddit =====
class FetchError(Exception):
    """Reddit からフィードを取れなかった（混雑・一時的な拒否・通信エラーなど）"""


def _get(url, **kw):
    try:
        res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, **kw)
    except requests.RequestException as err:
        raise FetchError(f"通信エラー ({type(err).__name__})")
    if res.status_code != 200:
        raise FetchError(f"HTTP {res.status_code}")
    return res


def fetch_arctic():
    """Arctic Shift から新着を取り、共通の形（新しい順）にそろえる"""
    res = _get(ARCTIC_URL, params={"subreddit": SUBREDDIT, "limit": ARCTIC_LIMIT, "sort": "desc"})
    try:
        posts = res.json()["data"]
    except (ValueError, KeyError, TypeError):
        raise FetchError("Arctic Shift の応答が想定外の形式です")
    items = []
    for p in posts:
        if not p.get("id"):
            continue
        body = p.get("selftext") or ""
        if body in ("[removed]", "[deleted]"):
            body = ""
        if not p.get("is_self") and p.get("url"):
            body = (body + " " + p["url"]).strip()  # リンク投稿は貼られたURLを本文に足す
        items.append({
            "id": "t3_" + p["id"],  # RSS と同じ ID 形式にそろえる（seen.json を共用するため）
            "title": p.get("title") or "",
            "body": body,
            "link": "https://www.reddit.com" + (p.get("permalink") or f"/comments/{p['id']}/"),
            "flair": p.get("link_flair_text"),
            "created": p.get("created_utc") or 0,
        })
    return items


def fetch_rss():
    """Reddit 本体の RSS から新着を取り、共通の形（新しい順）にそろえる"""
    url = FLAIR_FEED_URL if (FLAIR_FILTER and not os.environ.get("FEED_URL")) else FEED_URL
    entries = feedparser.parse(_get(url).content).entries
    return [{
        "id": e.id,
        "title": e.get("title", ""),
        "body": clean_text(e.get("summary") or (e.get("content") or [{}])[0].get("value", "")),
        "link": e.get("link", ""),
        "flair": None,  # RSS にはフレアが載らない（検索 RSS 側で絞り込み済み）
        "created": calendar.timegm(e.published_parsed) if e.get("published_parsed") else 0,
    } for e in entries]


def fetch_entries():
    """(取得元の名前, 投稿リスト) を返す。arctic が駄目なら rss に切り替える"""
    if SOURCE == "arctic":
        try:
            return "arctic", fetch_arctic()
        except FetchError as err:
            print(f"Arctic Shift から取得できませんでした（{err}）→ Reddit の RSS に切り替えます")
    return "rss", fetch_rss()


def flair_ok(item):
    if not FLAIR_FILTER or item["flair"] is None:  # フレア情報がない（RSS）は絞り込み済みとみなす
        return True
    return item["flair"] in FLAIR_FILTER


def clean_text(raw):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # Reddit RSS 末尾の "submitted by /u/xxx [link] [comments]" を除去
    text = re.sub(r"submitted by\s+/u/\S+.*$", "", text).strip()
    return text


def title_key(title):
    """[Rumor] などのタグ・大文字小文字・記号・空白の違いを無視したタイトルの比較用キー"""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title.lower())
    return re.sub(r"[^0-9a-z]+", "", t)


# ===== 日本語化（Gemini / Claude） =====
class OutOfTime(Exception):
    """この実行の持ち時間切れ（失敗回数には数えない）"""


_no_thinking_cfg = set()  # thinkingConfig を受け付けなかったモデル
_skip_models = set()      # この実行中は使わないモデル（上限到達・応答なし）


def ask_gemini(prompt, deadline=None):
    models = [GEMINI_MODEL] + [m.strip() for m in GEMINI_FALLBACK_MODELS.split(",") if m.strip()]
    models = list(dict.fromkeys(models))  # 重複を除く
    errors = []
    for model in models:
        if model in _skip_models:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        attempt = 0
        while attempt < 2:
            if deadline and time.time() > deadline:
                raise OutOfTime()
            gen = {"responseMimeType": "application/json", "temperature": 0.3}
            if GEMINI_THINKING and model not in _no_thinking_cfg:
                gen["thinkingConfig"] = {"thinkingLevel": GEMINI_THINKING}
            body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
            try:
                res = requests.post(url, json=body, timeout=60,
                                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
            except requests.RequestException as err:
                errors.append(f"{model}: 応答なし ({type(err).__name__})")
                print(f"  {model} の応答が遅すぎて打ち切りました")
                attempt += 1
                if attempt >= 2:
                    _skip_models.add(model)
                continue
            if res.status_code == 200:
                parts = res.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                print(f"  {model} で翻訳しました")
                return text
            reason = res.text[:200].replace("\n", " ")
            errors.append(f"{model}: HTTP {res.status_code} {reason}")
            if res.status_code == 400 and "thinking" in res.text.lower() and model not in _no_thinking_cfg:
                _no_thinking_cfg.add(model)  # 考える量の指定に非対応 → 指定なしでやり直す
                continue
            if res.status_code == 429:
                print(f"  {model} は無料枠の上限です → 今回の実行では次のモデルを使います")
                _skip_models.add(model)
                break
            if res.status_code in (500, 503, 504):
                print(f"  {model} が混雑中 (HTTP {res.status_code})")
                attempt += 1
                time.sleep(10)
                continue
            print(f"  {model} でエラー (HTTP {res.status_code})")
            break  # モデル名の誤りなど、待っても直らないエラー
    raise RuntimeError(" / ".join(errors))


def ask_claude(prompt):
    import anthropic  # Claude を使うときだけ読み込む

    msg = anthropic.Anthropic().messages.create(  # ANTHROPIC_API_KEY を環境変数から読む
        model=CLAUDE_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def summarize(title, body, deadline=None):
    prompt = PROMPT.format(title=title, body=body[:2000] or "(本文なし)")
    text = ask_claude(prompt) if TRANSLATOR == "claude" else ask_gemini(prompt, deadline)
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return data["title_ja"], data["summary_ja"], data["tweet"]


# ===== Discord =====
def post_discord(title_ja, summary_ja, url, original_title, flair=None):
    footer = f"[{flair}] 原文: {original_title}" if flair else f"原文: {original_title}"
    payload = {
        "embeds": [{
            "title": (title_ja or original_title or "(無題)")[:256],
            "url": url,
            "description": (summary_ja or "(本文なし)")[:4000],
            "footer": {"text": footer[:2048]},
            "color": 0xFF4500,
        }]
    }
    for _ in range(3):
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        if res.status_code == 429:
            time.sleep(float(res.json().get("retry_after", 2)))
            continue
        res.raise_for_status()
        return
    raise RuntimeError("Discord への送信がレート制限で失敗しました")


# ===== X =====
def x_weight(s):
    # X の文字数カウント：全角=2、半角=1（上限280）
    return sum(1 if ord(c) < 0x1100 else 2 for c in s)


def build_tweet(tweet):
    footer = f"\n\n出典: {SOURCE_LABEL}"
    while x_weight(tweet + footer) > 280:
        tweet = tweet[:-2] + "…"
    return tweet + footer


def post_x(text):
    import tweepy  # X 無効時は読み込まない

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    client.create_tweet(text=text)


# ===== メイン =====
def main():
    started = time.time()
    state = load_state()
    try:
        source, entries = fetch_entries()
    except FetchError as err:
        if state is None:
            sys.exit(f"Reddit からフィードを取れませんでした: {err}")
        streak = state.get("fetch_fail_streak", 0) + 1
        state["fetch_fail_streak"] = streak
        save_state(state)
        if streak >= FETCH_ALERT_AFTER:
            # 長く続くときだけ赤い×にして知らせる（本当にブロックされている可能性）
            sys.exit(f"Reddit から {streak} 回連続で取得できません: {err}。"
                     "README の「Reddit に弾かれた場合」を参照してください。")
        print(f"Reddit から取得できませんでした（{err}、{streak} 回目）。次回の実行で取り直します。")
        return
    if state is not None and state.get("fetch_fail_streak"):
        print(f"Reddit からの取得が回復しました（{state['fetch_fail_streak']} 回失敗のあと）")
        state["fetch_fail_streak"] = 0
        save_state(state)

    if state is None:
        # 初回は、いま見えている投稿を既読にするだけ（過去分を一気に流さない）
        # 例: arctic は直近約1週間・100件を返すので、そのまま流すと Leak だけで数十件が一度に届く
        now = time.time()
        state = {"seen": [e["id"] for e in reversed(entries)], "fails": {},
                 "titles": {title_key(e["title"]): now for e in entries},
                 "source": source, "last_ok": now}
        save_state(state)
        print(f"初回実行：{len(entries)} 件を通知せず既読登録しました。次回から新着を通知します。")
        return

    if state.get("source") != source:
        # 取得元が変わった（arctic ⇄ rss）。前回の実行より十分古い投稿は前の取得元で処理済みとみなし、
        # 既読にするだけにする。それより新しい投稿は通常どおり処理する（同じタイトルの重複は下で弾く）
        cutoff = state.get("last_ok", time.time()) - SWITCH_MARGIN
        known = set(state["seen"])
        old = [e["id"] for e in reversed(entries)
               if e["id"] not in known and (not e["created"] or e["created"] < cutoff)]
        state["seen"].extend(old)
        print(f"取得元が {state.get('source') or 'rss'} → {source} に変わりました。"
              f"前回より古い {len(old)} 件は通知せず既読にします。")
        state["source"] = source
        save_state(state)

    seen, fails, titles = state["seen"], state["fails"], state["titles"]
    seen_set = set(seen)
    unseen = [e for e in reversed(entries) if e["id"] not in seen_set]  # 古い順
    skipped = [e for e in unseen if not flair_ok(e)]
    for e in skipped:  # 対象外のフレアは既読にするだけ
        seen.append(e["id"])
    if skipped:
        save_state(state)
    new_entries = [e for e in unseen if flair_ok(e)][:MAX_POSTS_PER_RUN]
    state["last_ok"] = time.time()
    save_state(state)
    print(f"取得元 {source}：新着 {len(new_entries)} 件（対象外のフレア {len(skipped)} 件は既読のみ）")

    for e in new_entries:
        if time.time() - started > RUN_TIME_BUDGET:
            print("時間切れのため、残りは次回の実行で処理します")
            break

        title = e["title"]
        body = e["body"]
        link = e["link"]
        print(f"- [{e['flair'] or '-'}] {title[:60]}")

        key = title_key(title)
        if key and time.time() - titles.get(key, 0) < TITLE_DEDUP_DAYS * 86400:
            print("  同じタイトルの投稿を最近送ったのでスキップ（再投稿・重複投稿）")
            seen.append(e["id"])
            save_state(state)
            continue

        try:
            title_ja, summary_ja, tweet = summarize(title, body, started + RUN_TIME_BUDGET)
        except OutOfTime:
            print("時間切れのため、残りは次回の実行で処理します")
            break
        except Exception as err:
            count = fails.get(e["id"], 0) + 1
            print(f"  翻訳失敗 {count}/{MAX_TRANSLATE_TRIES} 回目: {err}")
            if count < MAX_TRANSLATE_TRIES:
                fails[e["id"]] = count
                save_state(state)
                continue  # 送らずに次回の実行で再挑戦
            # 何度やってもダメなら、英語のまま印を付けて送る
            title_ja, summary_ja, tweet = f"【未翻訳】{title}", body[:200], None

        try:
            post_discord(title_ja, summary_ja, link, title, e["flair"])
        except Exception as err:
            print(f"  Discord 送信失敗、次回再試行: {err}")
            continue  # 既読にしない

        if ENABLE_X and tweet:
            try:
                post_x(build_tweet(tweet))
            except Exception as err:
                print(f"  X 投稿失敗: {err}")

        seen.append(e["id"])
        if key:
            titles[key] = time.time()
        fails.pop(e["id"], None)
        # フィードから消えた古い投稿の失敗記録は掃除する
        live = {x["id"] for x in entries}
        for k in [k for k in fails if k not in live]:
            fails.pop(k)
        save_state(state)
        time.sleep(13 if TRANSLATOR == "gemini" else 1)  # Flash 系は1分5回まで


if __name__ == "__main__":
    main()
