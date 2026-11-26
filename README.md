<div align="center">
  <h1>GNU-Alarm · 경상국립대학교 공지사항 알림봇</h1>
  <p>경상국립대학교 AI정보공학과 박정준</p>
</div>

경상국립대학교 공식 게시판 다섯 곳을 확인하고, 새 게시물의 제목과 등록일을 Telegram으로 보냅니다. 제목을 누르면 학교 사이트의 공지 원문이 열립니다.

## 대상 게시판

| Telegram 구분 | 공식 게시판 | 발송 방식 |
| --- | --- | --- |
| 학사공지 | [학사](https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?mi=1127&bbsId=1029) | 일반 |
| 장학공지 | [장학](https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?mi=1376&bbsId=1075) | 일반 |
| 컴퓨터공학부 공지 | [학부 공지사항](https://www.gnu.ac.kr/cse/na/ntt/selectNttList.do?mi=17093&bbsId=4753) | 일반 |
| 일반공지 | [교내기관](https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?mi=1126&bbsId=1028) | 일반 |
| 행사공지 | [외부기관](https://www.gnu.ac.kr/main/na/ntt/selectNttList.do?mi=1132&bbsId=1033) | 무음 |

## 신규 공지 판정과 발송

- 학교 사이트의 게시물 번호(`nttSn`)와 게시판 번호(`bbsId`)로 글을 식별합니다. 제목 수정, 고정 상태 변경, 정렬 변경, ‘새로운 글’ 표시 만료 후에도 기존 게시물은 같은 글로 처리합니다. 새로 게시된 고정 공지는 알림에 포함합니다.
- 게시판별 첫 페이지에서 일반 글 최대 50건과 고정 공지를 읽습니다. 이전에 확인한 일반 글이 한 건도 없으면 최대 10페이지까지 확인합니다. 수집 실패는 해당 게시판의 상태를 보존하고 다른 게시판의 수집을 계속합니다.
- 최초 실행과 기존 해시 방식에서 전환하는 첫 실행에는 현재 목록을 발송 기준으로만 저장합니다. 그날부터 확인한 새 게시물은 제목을 묶어 최대 여섯 건씩 보냅니다. 기준일 이전에 등록된 글이 나중에 다시 고정돼도 발송하지 않습니다.
- 발송 대기 목록을 먼저 `state.json`에 기록하고 Telegram이 성공 응답을 준 메시지마다 발송 상태를 갱신합니다. GitHub Actions에서 상태를 원격 저장소에 반영합니다. Telegram이 메시지를 받았으나 응답이 유실되거나 상태 저장이 실패하면 다음 실행에서 해당 메시지가 중복될 수 있습니다.
- GitHub Actions는 매시 17분과 47분에 실행을 예약합니다. GitHub의 스케줄 지연에 따라 실제 실행 시간이 달라질 수 있습니다.

## 실행과 확인

저장소의 **Settings → Secrets and variables → Actions** 에 `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`를 등록합니다. **Actions → Watch GNU Notices → Run workflow** 에서 기본 옵션으로 수집만 확인하거나 발송 옵션을 활성화할 수 있습니다. 사용 중인 `state.json`에는 본 공지의 ID와 미전송 공지가 들어 있으므로 전송 이력을 유지하려면 해당 파일을 보존합니다.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/watch_notices.py --preview
```

## 연락처

- 개발자 박정준 ([LinkedIn](https://www.linkedin.com/in/jeong-jun-park/))
- 전자우편 [cluster@gnu.ac.kr](mailto:cluster@gnu.ac.kr)
