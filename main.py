import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
import urllib3
import ssl
import re
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from openai import OpenAI  # 💡 OpenAI 최신 규격 라이브러리 로드

# 1. 환경 설정 및 클라이언트 초기화
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"
db = firestore.Client()
events_ref = db.collection("events")
logs_ref = db.collection("crawler_logs")

# GitHub Actions 환경변수 또는 로컬 환경변수에서 API Key 자동 로드
ai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def clean_text(text):
    """인코딩 깨짐 및 유령 공백 문자를 완전히 박멸하는 함수"""
    if not text:
        return ""
    cleaned = re.sub(r'[\s\xa0\xad\t\n\r]+', ' ', text)
    return cleaned.strip()


def summarize_business_with_ai(stock_name, business_raw_text):
    """
    [🤖 AI 요약 엔진] GPT에게 원문 텍스트를 전달하여
    핵심 비즈니스 모델만 50~60자 내외로 자연스럽게 한 줄 요약해 옵니다.
    """
    if not business_raw_text or "사업현황" not in business_raw_text:
        return f"{stock_name} 상장 일정 자동 등록"

    try:
        # 프롬프트 엔지니어링을 통해 노이즈를 걸러내고 핵심 결과값 규격 강제
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",  # 가성비 극대화 모델 (회당 약 0.1원 미만)
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 주식 증권사 리서치 애널리스트야. 제공된 공모 기업의 사업 현황을 읽고, "
                        "증권사 리포트에서 사용하는 사업 분야를 한 줄로 요약해줘.\n"
                        "조건:\n"
                        "1. 핵심 기술과 적용 산업만 포함한다.\n"
                        "2. 고객사, 경쟁력, 수익구조, 성장전략, 시장 전망은 제외한다.\n"
                        "3. '당사는', '동사는' 등 주어는 사용하지 않는다.\n"
                        "4. 기업명은 포함하지 않는다.\n"
                        "5. '입니다' 대신 명사형으로 끝낸다.\n"
                        "6. 10~20자 내외로 작성한다.\n"
                        "7. '인공지능'키워드는 'AI'로 대체해줘\n"
                        "8. 설명 없이 결과 한 줄만 출력한다.\n"

                    )
                },
                {"role": "user", "content": f"기업명: {stock_name}\n\n[사업 현황 원문]\n{business_raw_text[:2500]}"}
                # 안정적 컨텍스트 전송
            ],
            max_tokens=100,
            temperature=0.4  # 일관성 있는 분석 출력을 위해 낮은 값 세팅
        )
        ai_result = response.choices[0].message.content.strip()
        return f"{ai_result}"

    except Exception as e:
        print(f"❌ GPT API 통신 실패: {str(e)}")
        # API 오류 등 비상 상황 발생 시 글자 자르기 폴백 로직으로 우회 처리 (안전장치)
        return f"[기업 개요] {stock_name} 공모주 신규상장 예정 종목."


def run_stock_crawler():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 GPT AI 엔진 장착 크롤러 가동...")

    base_url = "https://www.38.co.kr"
    list_url = f"{base_url}/html/fund/index.htm?o=nw"
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

        response = session.get(list_url, headers=headers, verify=False)
        response.encoding = 'euc-kr'

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table", {"summary": "신규상장종목"})
        if not table:
            raise Exception("상장 일정 테이블 요소를 찾을 수 없습니다.")

        rows = table.find_all("tr")
        success_count = 0
        skip_count = 0
        current_year = datetime.now().year

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 4:
                continue

            stock_a = cols[0].find("a")
            if not stock_a or not stock_a.get("href"):
                continue

            stock_name = clean_text(stock_a.text)
            raw_date = clean_text(cols[1].text).replace(".", "/")

            if not stock_name or not raw_date or raw_date in ["-", "미정", "상장일"] or "종목명" in stock_name:
                continue

            stock_name = stock_name.split("(")[0].strip()
            stock_name = stock_name.replace("(주)", "").replace("주식회사", "").strip()

            try:
                if len(raw_date.split('/')[0]) == 4:
                    date_obj = datetime.strptime(raw_date, "%Y/%m/%d")
                else:
                    date_obj = datetime.strptime(f"{current_year}/{raw_date}", "%Y/%m/%d")
                formatted_date = date_obj.strftime("%Y-%m-%d")
            except ValueError:
                continue

            duplicate_query = events_ref.where(
                filter=FieldFilter("date", "==", formatted_date)
            ).where(
                filter=FieldFilter("eventName", "==", stock_name)
            ).get()

            if len(duplicate_query) > 0:
                skip_count += 1
                continue

            detail_route = stock_a.get("href")
            if "index.htm" in detail_route:
                detail_route = detail_route.replace("index.htm", "").replace("?", "")

            detail_url = base_url + detail_route if detail_route.startswith(
                "/") else f"{base_url}/html/fund/{detail_route}"
            if "o=v" not in detail_url and "no=" in detail_url:
                detail_url = detail_url.replace("?", "?o=v&")

            detail_desc = "신규상장 예정 종목"

            try:
                detail_res = session.get(detail_url, headers=headers, verify=False)
                detail_res.encoding = 'euc-kr'

                if detail_res.status_code == 200:
                    detail_soup = BeautifulSoup(detail_res.text, "html.parser")
                    page_text = detail_soup.get_text()

                    if "1." in page_text or "사업현황" in page_text:
                        cleaned_page_text = clean_text(page_text)
                        start_idx = cleaned_page_text.find("1.")
                        if start_idx == -1:
                            start_idx = cleaned_page_text.find("사업현황")

                        if start_idx != -1:
                            target_chunk = cleaned_page_text[max(0, start_idx - 20):start_idx + 3500]
                            # 🎯 AI 요약 엔진 가동 처리
                            detail_desc = summarize_business_with_ai(stock_name, target_chunk)
            except Exception as sub_e:
                print(f"⚠️ {stock_name} AI 데이터 분석 위임 실패: {str(sub_e)}")

            payload = {
                "date": formatted_date,
                "category": "신규상장",
                "eventName": stock_name,
                "detail": detail_desc,
                "relatedStocks": ""
            }

            events_ref.add(payload)
            success_count += 1
            print(f"🤖 AI 요약 완료 및 등록 성공: {formatted_date} | {stock_name}")

        log_payload = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "SUCCESS",
            "added_count": success_count,
            "skipped_count": skip_count,
            "message": f"AI 수집 자동화 정상 종료 - 신규: {success_count}건"
        }
        logs_ref.add(log_payload)
        print(f"🏁 AI 파이프라인 프로세스 종료. 추가: {success_count}건 / 스킵: {skip_count}건")

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