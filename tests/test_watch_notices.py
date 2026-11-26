import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from scripts import watch_notices as bot


TODAY = "2026-11-26"


def row(post_id, number, title, posted_on="2026.11.26", badge=False):
    new_badge = '<i class="new_cont"></i><span class="sr-only">새로운 글</span>' if badge else ""
    return (
        f'<tr><td>{number}</td><td><a href="javascript:" class="nttInfoBtn" data-id="{post_id}">'
        f'{title}{new_badge}</a></td><td>학과</td><td>{posted_on}</td><td>12</td></tr>'
    )


def page(*rows, next_page=None):
    pagination = f'<a onclick="goPaging({next_page})">{next_page}</a>' if next_page else ""
    return (
        '<table><thead><tr><th>번호</th><th>제목</th><th>작성자</th><th>등록일</th><th>조회</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>{pagination}'
    )


class NoticeWatcherTests(unittest.TestCase):
    def test_page_uses_post_id_and_ignores_new_badge(self):
        html = page(row("101", "공지", "11.26. 학부 &amp; 대학원", badge=True),
                    row("102", "861", "2026.11.26"), next_page=2)
        notices, has_next = bot.parse_page(html, 1)
        self.assertTrue(has_next)
        self.assertEqual(notices, [
            bot.Notice("101", "11.26. 학부 & 대학원", TODAY, True),
            bot.Notice("102", "2026.11.26", TODAY, False),
        ])

    def test_empty_notice_list_is_distinct_from_wrong_markup(self):
        empty, has_next = bot.parse_page(page('<tr><td colspan="5">등록된 게시물이 없습니다.</td></tr>'), 1)
        self.assertEqual((empty, has_next), ([], False))
        for html in ("<html>점검 중</html>", page(), page(row("broken", "공지", "제목"))):
            with self.subTest(html=html), self.assertRaises(bot.WatchError):
                bot.parse_page(html, 1)

    def test_fixed_header_does_not_hide_new_items_or_trigger_repeat(self):
        initial, _ = bot.parse_page(page(row("1", "공지", "고정 공지", badge=True),
                                         row("2", "31", "이전 일반 공지")), 1)
        state, count = bot.update_history(None, initial, ["2"], TODAY)
        self.assertEqual(count, 0)
        later, _ = bot.parse_page(page(row("1", "공지", "고정 공지 제목 수정"),
                                       row("3", "공지", "새로 등록한 고정 공지", badge=True),
                                       row("4", "32", "새 일반 공지", badge=True),
                                       row("2", "30", "이전 일반 공지")), 1)
        state, count = bot.update_history(state, later, ["4", "2"], TODAY)
        self.assertEqual(count, 2)
        self.assertEqual({entry["id"] for entry in state["pending"]}, {"3", "4"})
        state["pending"].clear()
        reordered = [bot.Notice("3", "고정 해제", TODAY, False), *reversed(later)]
        state, count = bot.update_history(state, reordered, ["3", "2", "4"], TODAY)
        self.assertEqual(count, 0)
        self.assertEqual(state["pending"], [])

    def test_legacy_state_migrates_without_replaying_old_posts(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"boards": {"old": {"latest_id": "abc"}}}), encoding="utf-8")
            legacy = bot.load_state(path)
        self.assertEqual(legacy["boards"], {})
        history, count = bot.update_history(None, [bot.Notice("1", "기존", TODAY, False)], ["1"], TODAY)
        self.assertEqual(count, 0)
        history, count = bot.update_history(
            history,
            [bot.Notice("2", "예전 글 재고정", "2025-07-16", True),
             bot.Notice("3", "당일 신규", TODAY, True)],
            [],
            TODAY,
        )
        self.assertEqual(count, 1)
        self.assertEqual([entry["id"] for entry in history["pending"]], ["3"])
        self.assertIn("2", history["seen"])

    def test_catchup_skips_repeated_pins_until_regular_overlap(self):
        board = bot.BOARDS[0]
        pinned = row("1", "공지", "고정 공지")
        history = {"recent_regular": ["2"]}
        with patch.object(bot, "fetch_html", side_effect=[
            page(pinned, row("4", "12", "신규 일반"), next_page=2),
            page(pinned, row("3", "11", "또 다른 신규"), row("2", "10", "기존 일반")),
        ]) as fetch:
            notices, regular, pages = bot.scan_board(Mock(), board, history)
        self.assertEqual([notice.post_id for notice in notices], ["1", "4", "3", "2"])
        self.assertEqual((regular, pages), (["4"], 2))
        self.assertEqual(fetch.call_args_list[1].args[1], board.list_url(2))

    def test_catchup_rejects_ignored_pagination(self):
        html = page(row("8", "12", "신규"), next_page=3)
        with patch.object(bot, "fetch_html", return_value=html), self.assertRaises(bot.WatchError):
            bot.scan_board(Mock(), bot.BOARDS[0], {"recent_regular": ["2"]})

    def test_digest_escapes_content_and_links_to_cse_detail(self):
        board = bot.BOARDS[2]
        notice = {"id": "123", "title": '<AI> & "SW"', "posted_on": TODAY}
        digest = bot.format_digest([(board, notice)])
        self.assertIn("컴퓨터공학부 공지", digest)
        self.assertIn("&lt;AI&gt; &amp; &quot;SW&quot;", digest)
        self.assertIn("cse/na/ntt/selectNttInfo.do?mi=17093&amp;bbsId=4753&amp;nttSn=123", digest)
        self.assertNotIn("listCo", digest)

    def test_successful_batches_are_checkpointed_before_retry(self):
        main, event = bot.BOARDS[0], bot.BOARDS[-1]
        normal = [{"id": str(n), "title": "학사", "posted_on": TODAY} for n in range(7)]
        silent = [{"id": "31", "title": "행사", "posted_on": TODAY}]
        state = {"version": 2, "boards": {main.bbs_id: {"pending": normal}, event.bbs_id: {"pending": silent}}}

        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch.object(bot, "telegram_send", side_effect=[None, bot.WatchError("fail")]) as send:
                with self.assertRaises(bot.WatchError):
                    bot.send_pending(state, lambda: bot.save_state(path, state))
                self.assertEqual(send.call_count, 2)
            restored = bot.load_state(path)
            self.assertEqual([entry["id"] for entry in restored["boards"][main.bbs_id]["pending"]], ["6"])
            with patch.object(bot, "telegram_send") as send:
                bot.send_pending(restored, lambda: bot.save_state(path, restored))
                self.assertEqual([call.args[1] for call in send.call_args_list], [False, True])
            self.assertEqual(bot.load_state(path)["boards"][event.bbs_id]["pending"], [])

    def test_preview_and_partial_board_failure_do_not_consume_notices(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch.object(bot, "scan_board", side_effect=[
                requests.ConnectionError("offline"), *[([], [], 1)] * 4,
            ]), patch.object(bot, "telegram_send") as send:
                self.assertEqual(bot.run(path, preview=True), 1)
                self.assertFalse(path.exists())
                send.assert_not_called()
            with patch.object(bot, "scan_board", side_effect=[
                requests.ConnectionError("offline"), *[([], [], 1)] * 4,
            ]):
                self.assertEqual(bot.run(path), 1)
                boards = bot.load_state(path)["boards"]
                self.assertNotIn(bot.BOARDS[0].bbs_id, boards)
                self.assertIn(bot.BOARDS[2].bbs_id, boards)


if __name__ == "__main__":
    unittest.main()
