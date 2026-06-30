import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
import urllib3
import ssl
import re
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter # 💡 최신 필터 모듈 추가

# 1. 파이어베이스 인증 및 마스터 키 연결
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"
db = firestore.Client()
events_ref = db.collection("events")
logs_ref = db.collection("crawler_logs")


def clean_text(text):
    """38커뮤니케이션 특유의 인코딩 깨짐 및 유령 공백 문자를 완전히 박멸하는 함수"""
    if not text:
        return ""
    cleaned = re.sub(r'[\s\xa0\xad\t\n\r]+', ' ', text)
    return cleaned.strip()


def run_stock_crawler():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 크롤링 자동 파이프라인 가동...")

    url = "https://www.38.co.kr/html/fund/index.htm?o=nw"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    class SSLAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.set_ciphers('DEFAULT:@SECLEVEL=0')
            kwargs['ssl_context'] = context
            return super(SSLAdapter, self).init_poolmanager(*args, **kwargs)

    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        session = requests.Session()
        session.mount("https://", SSLAdapter())

        response = session.get(url, headers=headers, verify=False)
        response.encoding = 'euc-kr'

        if response.status_code != 200:
            raise Exception(f"사이트 접속 실패 (Status Code: {response.status_code})")

        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.find("table", {"summary": "신규상장종목"})
        if not table:
            raise Exception("상장 일정 테이블 요소를 찾을 수 없습니다. 웹사이트 구조 변경 의심.")

        rows = table.find_all("tr")

        success_count = 0
        skip_count = 0
        current_year = datetime.now().year

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 4:
                continue

            stock_name = clean_text(cols[0].text)
            raw_date = clean_text(cols[1].text).replace(".", "/")

            if not stock_name or not raw_date or raw_date in ["-", "미정", "상장일"] or "종목명" in stock_name:
                continue

            # 주식회사, 괄호 조건 등의 부가 정보 정제
            stock_name = stock_name.split("(")[0].strip()
            stock_name = stock_name.replace("(주)", "").replace("주식회사", "").strip()

            # 💡 [핵심 해결 지점] 연도가 이미 포함되어 들어오는지 여부에 따른 유연한 날짜 포맷 변환
            formatted_date = ""
            try:
                if len(raw_date.split('/')[0]) == 4:
                    # 1. 연도가 포함된 경우 (예: "2026/03/27") -> 그대로 파싱
                    date_obj = datetime.strptime(raw_date, "%Y/%m/%d")
                else:
                    # 2. 월/일만 있는 경우 (예: "03/27") -> 현재 연도 보정 조립
                    date_obj = datetime.strptime(f"{current_year}/{raw_date}", "%Y/%m/%d")

                formatted_date = date_obj.strftime("%Y-%m-%d")
            except ValueError:
                # 파싱 불가 날짜형태 예외 차단
                continue

            # DB 내 중복 적재 선제 방어
            duplicate_query = events_ref.where(
                filter=FieldFilter("date", "==", formatted_date)
            ).where(
                filter=FieldFilter("eventName", "==", stock_name)
            ).get()

            if len(duplicate_query) > 0:
                skip_count += 1
                continue

            payload = {
                "date": formatted_date,
                "category": "신규상장",
                "eventName": stock_name,
                "detail": "38커뮤니케이션 데이터 기반 신규상장 일정 자동 등록",
                "relatedStocks": stock_name
            }

            events_ref.add(payload)
            success_count += 1
            print(f"✔ 신규 등록 성공: {formatted_date} | {stock_name}")

        log_payload = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "SUCCESS",
            "added_count": success_count,
            "skipped_count": skip_count,
            "message": f"정상 종료 - 신규 등록: {success_count}건, 중복 스킵: {skip_count}건"
        }
        logs_ref.add(log_payload)
        print(f"🏁 파이프라인 종료 완료. 추가: {success_count}건 / 스킵: {skip_count}건")

    except Exception as e:
        error_msg = str(e)
        print(f"❌ 에러 발생: {error_msg}")
        logs_ref.add({
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "FAILED",
            "added_count": 0,
            "skipped_count": 0,
            "message": f"크롤링 실패 에러 로그: {error_msg}"
        })


if __name__ == "__main__":
    run_stock_crawler()