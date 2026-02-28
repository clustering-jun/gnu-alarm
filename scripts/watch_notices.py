import os
import json
import hashlib
from typing import Dict, List, Any, Tuple

import requests
from bs4 import BeautifulSoup

BOARDS = {
    "학사공지": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?mi=1127&bbsId=1029",
    "장학공지": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?bbsId=1075&mi=1376",
    "일반공지": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?bbsId=1028&mi=1126",
    "행사공지": "https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?bbsId=1033&mi=1132",
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


def extract_items(board_url: str, limit: int = 20) -> List[Dict[str, str]]:
    """
    GNU 게시판 목록에서 (title, date, link, id_guess) 추출.
    - HTML 구조가 바뀌면 여기 selector만 수정하면 됩니다.
    """
    html = fetch_html(board_url)
    soup = BeautifulSoup(html, "lxml")

    anchors = soup.select('a[href*="nttView.do"]')

    items: List[Dict[str, str]] = []
    seen = set()

    for a in anchors:
        title = a.get_text(strip=True)
        href = (a.get("href") or "").strip()
        if not title or not href:
            continue

        # 상대경로 보정
        if href.startswith("/"):
            link = "https://www.gnu.ac.kr" + href
        elif href.startswith("http"):
            link = href
        else:
            link = "https://www.gnu.ac.kr/main/na/" + href.lstrip("./")

        # 글 ID 추정: nttId 있으면 그걸 쓰고, 없으면 link 해시
        ntt_id = ""
        if "nttId=" in link:
            try:
                ntt_id = link.split("nttId=")[1].split("&")[0]
            except Exception:
                ntt_id = ""
        if not ntt_id:
            ntt_id = sha1(link)[:12]

        # 등록일: 같은 행(tr)에서 날짜 후보를 찾는 휴리스틱
        date_text = ""
        tr = a.find_parent("tr")
        if tr:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            for cell in tds[::-1]:
                if "-" in cell and len(cell) >= 10:
                    date_text = cell
                    break

        key = (title, link)
        if key in seen:
            continue
        seen.add(key)

        items.append({
            "id": ntt_id,
            "title": title,
            "date": date_text,
            "link": link,
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
