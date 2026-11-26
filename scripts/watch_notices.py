import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass(frozen=True)
class Board:
    name: str
    site: str
    bbs_id: str
    menu_id: str
    silent: bool = False

    def url(self, action, **params):
        query = urlencode({"mi": self.menu_id, "bbsId": self.bbs_id, **params})
        return f"https://www.gnu.ac.kr/{self.site}/na/ntt/{action}.do?{query}"

    def list_url(self, page):
        return self.url("selectNttList", listCo=50, currPage=page)

    def post_url(self, post_id):
        return self.url("selectNttInfo", nttSn=post_id)


BOARDS = (
    Board("학사공지", "main", "1029", "1127"),
    Board("장학공지", "main", "1075", "1376"),
    Board("컴퓨터공학부 공지", "cse", "4753", "17093"),
    Board("일반공지", "main", "1028", "1126"),
    Board("행사공지", "main", "1033", "1132", silent=True),
)
STATE_PATH = Path(__file__).resolve().parents[1] / "state.json"
STATE_VERSION = 2
KST = timezone(timedelta(hours=9))
MAX_PAGES = 10
POSTS_PER_MESSAGE = 6
TITLE_LIMIT = 300
DATE_PATTERN = re.compile(r"\d{4}\.\d{2}\.\d{2}")
PAGE_PATTERN = re.compile(r"\s*goPaging\((\d+)\)\s*;?\s*")
ID_PATTERN = re.compile(r"[0-9]+")


class WatchError(Exception):
    pass


@dataclass(frozen=True)
class Notice:
    post_id: str
    title: str
    posted_on: str
    pinned: bool

    def pending_entry(self):
        return {
            "id": self.post_id,
            "title": self.title,
            "posted_on": self.posted_on,
        }


def parse_page(html, page):
    soup = BeautifulSoup(html, "lxml")
    for table in soup.select("table"):
        headers = [cell.get_text(strip=True) for cell in table.select("thead th")]
        if {"제목", "등록일", "번호"} <= set(headers):
            break
    else:
        raise WatchError("공지 목록 테이블을 찾지 못했습니다.")

    number_column = headers.index("번호")
    date_column = headers.index("등록일")
    title_column = headers.index("제목")
    notices = []
    empty = False
    for row in table.select("tbody tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) == 1 and re.search(r"게시물.*없", cells[0].get_text(strip=True)):
            empty = True
            continue
        if len(cells) != len(headers):
            raise WatchError("공지 행의 열 개수가 목록 머리글과 다릅니다.")
        link = cells[title_column].select_one("a.nttInfoBtn[data-id]")
        post_id = link.get("data-id", "") if link else ""
        if not ID_PATTERN.fullmatch(post_id):
            raise WatchError("공지 행의 게시물 번호를 읽지 못했습니다.")

        posted_on = cells[date_column].get_text(strip=True)
        for badge in link.select(".new_cont, .sr-only"):
            badge.decompose()
        title = " ".join(link.get_text(" ", strip=True).split())
        if not DATE_PATTERN.fullmatch(posted_on) or not title:
            raise WatchError(f"게시물 {post_id}의 제목 또는 등록일을 읽지 못했습니다.")
        try:
            posted_on = date.fromisoformat(posted_on.replace(".", "-")).isoformat()
        except ValueError as exc:
            raise WatchError(f"게시물 {post_id}의 등록일 형식이 잘못되었습니다.") from exc
        notices.append(Notice(post_id, title, posted_on, cells[number_column].get_text(strip=True) == "공지"))

    if not notices and not empty:
        raise WatchError("목록에 게시물과 빈 목록 안내가 모두 없습니다.")
    has_next = any(
        int(match[1]) > page
        for link in soup.select("a[onclick]")
        if (match := PAGE_PATTERN.fullmatch(link["onclick"]))
    )
    return notices, has_next


def fetch_html(session, url):
    response = session.get(url, timeout=(8, 20))
    response.raise_for_status()
    return response.content


def scan_board(session, board, history):
    anchors = set(history["recent_regular"]) if history else set()
    collected = {}
    regular_on_first_page = []
    scanned_regular = set()

    for page in range(1, MAX_PAGES + 1):
        notices, has_next = parse_page(fetch_html(session, board.list_url(page)), page)
        regular = list(dict.fromkeys(item.post_id for item in notices if not item.pinned))
        if page == 1:
            regular_on_first_page = regular
        if page > 1 and regular and set(regular) <= scanned_regular:
            raise WatchError(f"{page}페이지가 앞 페이지의 게시물을 반복했습니다.")
        scanned_regular.update(regular)
        for item in notices:
            collected.setdefault(item.post_id, item)

        if not anchors or anchors.intersection(regular) or not has_next:
            return list(collected.values()), regular_on_first_page, page

    raise WatchError(f"{MAX_PAGES}페이지 안에서 이전 목록과 만나는 지점을 찾지 못했습니다.")


def load_state(path):
    if not path.exists():
        return {"version": STATE_VERSION, "boards": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("boards"), dict):
        raise WatchError("state.json의 게시판 상태 형식이 잘못되었습니다.")
    if "version" not in state:
        # 예전 상태에는 게시판별 해시 하나만 있어 이미 발송한 ID를 복원할 수 없다.
        return {"version": STATE_VERSION, "boards": {}}
    if state["version"] != STATE_VERSION:
        raise WatchError("지원하지 않는 state.json 버전입니다.")
    return state


def update_history(history, notices, regular, today):
    if history is None:
        return {
            "baseline_on": today,
            "seen": sorted({item.post_id for item in notices}),
            "recent_regular": regular,
            "pending": [],
        }, 0

    seen = set(history["seen"])
    pending = list(history["pending"])
    new_count = 0
    for item in sorted(notices, key=lambda notice: (notice.posted_on, int(notice.post_id)), reverse=True):
        if item.post_id in seen:
            continue
        if item.posted_on >= history["baseline_on"]:
            pending.append(item.pending_entry())
            new_count += 1
        seen.add(item.post_id)

    return {
        "baseline_on": history["baseline_on"],
        "seen": sorted(seen),
        "recent_regular": regular,
        "pending": pending,
    }, new_count


def save_state(path, state, commit=False):
    content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

    if commit:
        for command in (
            ["git", "add", "--", path.name],
            ["git", "commit", "-m", "Update notice state"],
            ["git", "push"],
        ):
            subprocess.run(command, cwd=path.parent, check=True)


def format_digest(entries):
    lines = [f"<b>경상국립대 새 공지 {len(entries)}건</b>"]
    previous_board = None
    for board, entry in entries:
        if board != previous_board:
            lines.extend(("", f"<b>{escape(board.name)}</b>"))
            previous_board = board
        title = entry["title"]
        if len(title) > TITLE_LIMIT:
            title = title[:TITLE_LIMIT - 1] + "…"
        url = escape(board.post_url(entry["id"]), quote=True)
        posted_on = entry["posted_on"][5:].replace("-", ".")
        lines.append(f'• <a href="{url}">{escape(title)}</a> ({posted_on})')
    return "\n".join(lines)


def telegram_send(text, silent):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise WatchError("Telegram 봇 토큰 또는 채팅 ID가 설정되지 않았습니다.")
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "disable_notification": silent,
            },
            timeout=(8, 25),
        )
        result = response.json()
    except (requests.RequestException, ValueError):
        raise WatchError("Telegram 응답을 확인하지 못했습니다.") from None
    if not response.ok or not isinstance(result, dict) or result.get("ok") is not True:
        raise WatchError(f"Telegram 전송이 거절되었습니다 (HTTP {response.status_code}).")


def send_pending(state, persist):
    boards_state = state["boards"]
    for silent in (False, True):
        entries = [
            (board, entry)
            for board in BOARDS if board.silent == silent and board.bbs_id in boards_state
            for entry in boards_state[board.bbs_id]["pending"]
        ]
        for offset in range(0, len(entries), POSTS_PER_MESSAGE):
            batch = entries[offset:offset + POSTS_PER_MESSAGE]
            telegram_send(format_digest(batch), silent)
            for board, entry in batch:
                boards_state[board.bbs_id]["pending"].remove(entry)
            persist()
            print(f"Telegram 전송 완료: {len(batch)}건 (무음: {silent})", flush=True)


def run(path, preview=False, commit=False):
    state = load_state(path)
    boards_state = state["boards"]
    today = datetime.now(KST).date().isoformat()
    errors = False

    with requests.Session() as session:
        session.headers["User-Agent"] = "GNU-Alarm (+https://github.com/clustering-jun/gnu-alarm)"
        retries = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
        session.mount("https://www.gnu.ac.kr/", HTTPAdapter(max_retries=retries))
        for board in BOARDS:
            history = boards_state.get(board.bbs_id)
            try:
                notices, regular, pages = scan_board(session, board, history)
            except (WatchError, requests.RequestException) as exc:
                print(f"{board.name}: 수집 실패 ({exc})", flush=True)
                errors = True
                continue
            boards_state[board.bbs_id], new_count = update_history(history, notices, regular, today)
            pinned = sum(item.pinned for item in notices)
            print(
                f"{board.name}: {pages}페이지 {len(notices)}건 수집 "
                f"(고정 {pinned}건), 신규 {new_count}건"
                + (" [기준 목록 등록]" if history is None else ""),
                flush=True,
            )

    if not preview and boards_state:
        def persist():
            save_state(path, state, commit)

        persist()
        try:
            send_pending(state, persist)
        except WatchError as exc:
            print(f"{exc} 미전송 공지는 다음 실행에서 다시 시도합니다.", flush=True)
            errors = True
    return int(errors)


def main():
    parser = argparse.ArgumentParser(description="경상국립대 공지 알림봇")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="게시판 수집만 검증합니다.")
    mode.add_argument("--commit-state", action="store_true", help="상태를 Git에 즉시 저장합니다.")
    args = parser.parse_args()
    return run(STATE_PATH, preview=args.preview, commit=args.commit_state)


if __name__ == "__main__":
    raise SystemExit(main())
