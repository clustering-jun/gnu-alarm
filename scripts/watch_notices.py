import os
import json
import hashlib
from typing import Dict, List, Any, Tuple

import requests
from bs4 import BeautifulSoup

BOARDS = {
    "학사공지": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?mi=1127&bbsId=1029",
    "장학공지": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?bbsId=1075&mi=1376",
    "교내기관": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?bbsId=1028&mi=1126",
    "교외기관": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?bbsId=1033&mi=1132",
}

STATE_PATH = "state.json"
FETCH_LIMIT = 20          # 각 게시판에서 가져오는 개수
FINGERPRINT_TOPN = 20     # 변경감지에 사용하는 개수(상위 N개)
TELEGRAM_MAX_LEN = 3900   # 안전 여유(4096 근처에서 분할)

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; NoticeWatcher/1.0; +https://github.com/)"
}


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"boards": {}}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def fetch_html(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


import re

DATE_RE = re.compile(r"\b(20\d{2}[.\-]\d{2}[.\-]\d{2})\b")

def extract_items(board_url: str, limit: int = 20) -> List[Dict[str, str]]:
    """
    GNU 게시판 목록에서 (title, date, id) 추출.
    - 링크(a href nttView.do)가 HTML에 직접 없어서,
      테이블(tr/td) 기반으로 제목/등록일을 뽑는 방식으로 구현.
    """
    html = fetch_html(board_url)
    soup = BeautifulSoup(html, "lxml")

    # 대부분 목록은 table 안에 있음
    rows = soup.select("table tbody tr")
    items: List[Dict[str, str]] = []

    for tr in rows:
        tds = tr.find_all("td")
        if not tds:
            continue

        cells = [td.get_text(" ", strip=True) for td in tds]
        cells = [c for c in cells if c]

        # 날짜 추출 (마지막 쪽 td에 있는 경우가 많음)
        date_text = ""
        for c in reversed(cells):
            m = DATE_RE.search(c)
            if m:
                date_text = m.group(1)
                break

        # 날짜/counter/짧은 토큰 제거 후 제목 후보 선정
        def is_noise(x: str) -> bool:
            if not x:
                return True
            if x.isdigit():
                return True
            if x in {"공지"}:
                return True
            # 전화번호/조회수 등 숫자 위주
            if len(re.sub(r"[0-9\s\-\(\)]", "", x)) == 0 and len(x) <= 6:
                return True
            # 날짜 토큰
            if DATE_RE.search(x):
                return True
            return False

        candidates = [c for c in cells if not is_noise(c)]
        if not candidates:
            continue

        # 제목은 “가장 긴 후보”로 잡는 휴리스틱 (공지 제목이 보통 가장 김)
        title = max(candidates, key=len)

        # 행 식별용 키(번호/구분이 있으면 포함)
        row_key = "|".join(cells[:3])  # 앞쪽 컬럼(번호/구분/일부정보)이 가장 변별력 있음
        stable_id = sha1(f"{board_url}|{row_key}|{title}|{date_text}")[:12]

        items.append({
            "id": stable_id,
            "title": title,
            "date": date_text,
            "link": "",  # 현재는 상세 링크를 안정적으로 구성하지 않음
        })

        if len(items) >= limit:
            break

    return items
    
def telegram_send_raw(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def send_telegram_split(message: str, max_len: int = TELEGRAM_MAX_LEN) -> None:
    """
    텔레그램 sendMessage 길이 제한(약 4096 chars) 때문에 자동 분할 전송.
    - "알림 개수 제한 없음" 요구를 만족시키기 위한 안전장치
    """
    if len(message) <= max_len:
        telegram_send_raw(message)
        return

    lines = message.splitlines()
    buf: List[str] = []
    cur = 0

    for line in lines:
        # +1은 개행 고려
        add_len = len(line) + (1 if buf else 0)
        if cur + add_len > max_len:
            telegram_send_raw("\n".join(buf))
            buf = [line]
            cur = len(line)
        else:
            if buf:
                cur += 1
            buf.append(line)
            cur += len(line)

    if buf:
        telegram_send_raw("\n".join(buf))


def format_changes_minimal(all_changes: List[Tuple[str, List[Dict[str, str]]]]) -> str:
    """
    포맷:
    ■ 학사공지
    - 제목 (등록일)
    - 제목 (등록일)

    게시판 URL/글 링크/상단 타임스탬프 없음.
    """
    lines: List[str] = []

    for board_name, new_items in all_changes:
        lines.append(f"■ {board_name}")
        for it in new_items:  # 제한 없음
            date = f' ({it["date"]})' if it.get("date") else ""
            lines.append(f'- {it["title"]}{date}')
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def main():
    state = load_state()
    boards_state = state.setdefault("boards", {})

    all_changes: List[Tuple[str, List[Dict[str, str]]]] = []

    for board_name, url in BOARDS.items():
        items = extract_items(url, limit=FETCH_LIMIT)

        # 변경감지 fingerprint: 상위 20개 사용
        top = items[:FINGERPRINT_TOPN]
        fp_src = "\n".join([f'{it["id"]}|{it["title"]}|{it["date"]}' for it in top])
        fp = sha1(fp_src)

        prev_fp = boards_state.get(board_name, {}).get("fingerprint")
        boards_state.setdefault(board_name, {})

        if prev_fp is None:
            # 최초 실행: 상태만 저장, 알림 생략
            boards_state[board_name]["fingerprint"] = fp
            boards_state[board_name]["latest_id"] = items[0]["id"] if items else ""
            continue

        if fp != prev_fp:
            prev_latest = boards_state[board_name].get("latest_id", "")
            new_items: List[Dict[str, str]] = []

            # prev_latest가 나올 때까지 = 새로 등장한 글
            for it in items:
                if it["id"] == prev_latest:
                    break
                new_items.append(it)

            # prev_latest를 못 찾으면(대량 변경/구조 변화/정렬 변화):
            # "변경됨"만 알리고 싶다면 여기 정책을 바꾸면 됩니다.
            # 요구사항에 "제한 없음"이므로, 기본은 20개 전체를 보냄.
            if not new_items:
                new_items = items[:]  # 20개 전부

            all_changes.append((board_name, new_items))

            boards_state[board_name]["fingerprint"] = fp
            boards_state[board_name]["latest_id"] = items[0]["id"] if items else ""

    if all_changes:
        msg = format_changes_minimal(all_changes)
        send_telegram_split(msg)

    save_state(state)


if __name__ == "__main__":
    main()
