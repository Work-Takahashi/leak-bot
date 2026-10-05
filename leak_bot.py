"""
Reddit (r/GamingLeaksAndRumours) の新着を取得し、
Gemini（無料枠）または Claude で日本語に要約して Discord に通知、(有効時は) X にも投稿するスクリプト。
GitHub Actions で定期実行する想定。
"""
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
FEED_URL = os.environ.get("FEED_URL") or "https://www.reddit.com/r/GamingLeaksAndRumours/new/.rss"
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TRANSLATOR = (os.environ.get("TRANSLATOR") or "gemini").lower()  # "gemini"（無料）または "claude"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL") or "claude-haiku-4-5-20251001"
ENABLE_X = (os.environ.get("ENABLE_X") or "false").lower() == "true"
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN") or "10")
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
def load_seen():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None  # 初回実行


def save_seen(ids):
    STATE_FILE.write_text(json.dumps(ids[-MAX_SEEN:], ensure_ascii=False, indent=1), encoding="utf-8")


# ===== Reddit =====
def fetch_entries():
    res = requests.get(FEED_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    if res.status_code in (403, 429):
        sys.exit(f"Reddit に拒否されました (HTTP {res.status_code})。README の「Reddit に弾かれた場合」を参照してください。")
    res.raise_for_status()
    return feedparser.parse(res.content).entries


def clean_text(raw):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # Reddit RSS 末尾の "submitted by /u/xxx [link] [comments]" を除去
    text = re.sub(r"submitted by\s+/u/\S+.*$", "", text).strip()
    return text


# ===== 日本語化（Gemini / Claude） =====
def ask_gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    for wait in (0, 20, 60):  # 無料枠の回数制限(429)に当たったら待って再試行
        time.sleep(wait)
        res = requests.post(url, json=body, timeout=60,
                            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        if res.status_code in (429, 503):
            continue
        res.raise_for_status()
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError(f"Gemini の回数制限に達しました (HTTP {res.status_code})")


def ask_claude(prompt):
    import anthropic  # Claude を使うときだけ読み込む

    msg = anthropic.Anthropic().messages.create(  # ANTHROPIC_API_KEY を環境変数から読む
        model=CLAUDE_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def summarize(title, body):
    prompt = PROMPT.format(title=title, body=body[:2000] or "(本文なし)")
    text = ask_claude(prompt) if TRANSLATOR == "claude" else ask_gemini(prompt)
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return data["title_ja"], data["summary_ja"], data["tweet"]


# ===== Discord =====
def post_discord(title_ja, summary_ja, url, original_title):
    payload = {
        "embeds": [{
            "title": (title_ja or original_title or "(無題)")[:256],
            "url": url,
            "description": (summary_ja or "(本文なし)")[:4000],
            "footer": {"text": f"原文: {original_title}"[:2048]},
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
    entries = fetch_entries()
    seen = load_seen()

    if seen is None:
        # 初回は既存の投稿を既読にするだけ（過去分を一気に流さない）
        save_seen([e.id for e in reversed(entries)])
        print(f"初回実行：{len(entries)} 件を既読登録しました。次回から新着を通知します。")
        return

    seen_set = set(seen)
    new_entries = [e for e in reversed(entries) if e.id not in seen_set][:MAX_POSTS_PER_RUN]  # 古い順
    print(f"新着 {len(new_entries)} 件")

    for e in new_entries:
        title = e.get("title", "")
        body = clean_text(e.get("summary") or (e.get("content") or [{}])[0].get("value", ""))
        link = e.get("link", "")

        try:
            title_ja, summary_ja, tweet = summarize(title, body)
        except Exception as err:  # 翻訳失敗時は英語のまま通知
            print(f"要約失敗 ({err}) → 原文で通知")
            title_ja, summary_ja, tweet = title, body[:200], None

        try:
            post_discord(title_ja, summary_ja, link, title)
        except Exception as err:
            print(f"Discord 送信失敗、次回再試行: {err}")
            continue  # 既読にしない

        if ENABLE_X and tweet:
            try:
                post_x(build_tweet(tweet))
            except Exception as err:
                print(f"X 投稿失敗: {err}")

        seen.append(e.id)
        save_seen(seen)
        time.sleep(7 if TRANSLATOR == "gemini" else 1)


if __name__ == "__main__":
    main()
