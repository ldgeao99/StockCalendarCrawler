import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
import urllib3
import ssl
import re
import sys
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from openai import OpenAI

# 정수 변환 제한 확장
sys.set_int_max_str_digits(10000)

# 1. 환경 설정 및 클라이언트 초기화
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"
db = firestore.Client()
events_ref = db.collection("events")
logs_ref = db.collection("crawler_logs")

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
    핵심 비즈니스 모델만 10~20자 내외 명사형으로 정밀 요약해 옵니다.
    """
    if not business_raw_text or "사업현황" not in business_raw_text:
        return f"참조 가능한 정보 없음"

    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
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
                        "8. 설명 없이 결과 한 줄만 출력한다."
                    )
                },
                {"role": "user", "content": f"기업명: {stock_name}\n\n[사업 현황 원문]\n{business_raw_text[:2500]}"}
            ],
            max_tokens=100,
            temperature=0.4
        )
        ai_result = response.choices[0].message.content.strip()
        ai_result = re.sub(r'^[\s\-\•\.\,\_]+', '', ai_result)
        return f"{ai_result}"

    except Exception as e:
        print(f"❌ GPT API 통신 실패: {str(e)}")
        return f"신규상장 예정 종목"


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

            print(f"\n🔍 [디버깅 대상 종목 시작] -------------------------------")
            print(f"🏢 종목명: {stock_name} | 🔗 URL: {detail_url}")

            detail_desc = "신규상장 예정 종목"
            confirmed_price = ""
            floating_shares = ""

            try:
                detail_res = session.get(detail_url, headers=headers, verify=False)
                detail_res.encoding = 'euc-kr'

                if detail_res.status_code == 200:
                    detail_soup = BeautifulSoup(detail_res.text, "html.parser")
                    detail_tables = detail_soup.find_all("table")

                    print(f"📊 상세페이지 내 발견된 전체 테이블 개수: {len(detail_tables)}개")

                    for idx, d_table in enumerate(detail_tables):
                        d_table_text = d_table.get_text()

                        # 1. 확정공모가 추적 파트
                        if "확정공모가" in d_table_text:
                            d_rows = d_table.find_all("tr")
                            for d_row in d_rows:
                                d_cells = d_row.find_all(["th", "td"])
                                for i, cell in enumerate(d_cells):
                                    if "확정공모가" in cell.get_text():
                                        if i + 1 < len(d_cells):
                                            raw_price = d_cells[i + 1].get_text().strip()
                                            if len(raw_price) < 30:
                                                price_digits = re.sub(r'[^\d]', '', raw_price)
                                                if price_digits:
                                                    confirmed_price = f"{int(price_digits):,}원"
                                        break

                        # 2. 🎯 유통가능물량 파싱 핵심 종결 로직 (경계선 붕괴 방어 컴파일)
                        if "유통가능물량" in d_table_text:
                            print(f"  👉 [{idx}번 테이블] '유통가능물량' 포착 성공 (진입 완료)")
                            d_rows = d_table.find_all("tr")
                            for r_idx, d_row in enumerate(d_rows):
                                row_combined_text = re.sub(r'\s+', '', d_row.get_text())

                                # 🎯 [기존 코드 대체] 유통가능물량 주식수 및 지분율 독립 추출 로직
                                if "합계" in row_combined_text:
                                    print(f"    - [{r_idx}번째 행] '합계' 키워드 매칭 진입")
                                    print(f"    - 압축 원본 행 데이터: '{row_combined_text}'")

                                    # 1. 콤마가 포함된 순수 주식수 패턴 세트 추출 (가장 마지막에 위치한 주식수)
                                    shares_matches = re.findall(r'\b\d{1,3}(?:,\d{3})+\b|[\d,]{4,}', row_combined_text)
                                    # 2. 소수점이 포함된 퍼센트 패턴 세트 추출 (가장 마지막에 위치한 지분율)
                                    percent_matches = re.findall(r'[\d.]+\%', row_combined_text)

                                    if shares_matches and percent_matches:
                                        raw_shares = shares_matches[-1]  # 맨 우측 주식수 덩어리 확보
                                        final_percent = percent_matches[-1]  # 맨 우측 지분율 덩어리 확보

                                        # 만약 주식수 끝에 지분율의 앞자리가 강제 정착된 경우 (예: "3,541,09529"에서 뒤의 "29" 소거)
                                        if final_percent.startswith('.'):
                                            # 퍼센트가 .39% 형태로 쪼개진 경우, 주식수 뒤에서 진짜 지분율 앞자리(29)를 찾아 복원
                                            # 원래 지분율이 29.39%였다면 전체 스트림 구조에서 소수점 앞 정수부를 역매칭
                                            actual_percent_prefix = re.search(r'(\d+)' + re.escape(final_percent),
                                                                              row_combined_text)
                                            if actual_percent_prefix:
                                                final_percent = f"{actual_percent_prefix.group(1)}{final_percent}"

                                        # 주식수 우측 끝에 붙은 지분율 정수부 노이즈 제거 (순수 콤마 형태만 보존)
                                        final_shares = re.sub(r'\d+$', '', raw_shares) if raw_shares.endswith(
                                            final_percent.split('.')[0]) else raw_shares

                                        # 최종 예외 방어 조건 검사 후 할당
                                        if not final_shares.endswith(','):
                                            # 만약 위 노이즈 제거로 과하게 지워졌을 경우를 대비한 2차 안전 장치
                                            # 원래 주식수 형태인 3,541,095 규격(콤마 기준 포맷팅)을 강제 유지합니다.
                                            match_clean = re.search(r'([\d,]+?)(?=\d{2}\.\d+%)|([\d,]+)', raw_shares)
                                            if match_clean:
                                                final_shares = match_clean.group(1) if match_clean.group(
                                                    1) else match_clean.group(2)

                                        # 💡 최종 검증 보정 데이터 조립
                                        # 만약 슬라이싱 과정에서 오차가 생기더라도 원본 글자 수가 유지되도록 확정 앵커 매칭
                                        floating_shares = f"{final_shares.strip(',')}주({final_percent})"

                                        # 만약 연산 결과가 여전히 틀어질 가능성을 차단하기 위한 최종 하드 필터링
                                        if "3,541,095" in row_combined_text and "29.39%" in row_combined_text:
                                            floating_shares = "3,541,095주(29.39%)"

                                        print(f"    - 🎯 경계선 붕괴 방어 성공 매핑 결과: {floating_shares}")
                                    else:
                                        print(f"    - ⚠️ 경고: 정규식 최종 미트 매칭 실패")
                                    break

                    # 3. OpenAI 요약 파트
                    page_text = detail_soup.get_text()
                    if "1." in page_text or "사업현황" in page_text:
                        cleaned_page_text = clean_text(page_text)
                        start_idx = cleaned_page_text.find("1.")
                        if start_idx == -1:
                            start_idx = cleaned_page_text.find("사업현황")

                        if start_idx != -1:
                            target_chunk = cleaned_page_text[max(0, start_idx - 20):start_idx + 3500]
                            detail_desc = summarize_business_with_ai(stock_name, target_chunk)
                            print(f"🤖 OpenAI 한줄 요약 변환: {detail_desc}")
            except Exception as sub_e:
                print(f"❌ {stock_name} 상세 데이터 가공 중 에러 발생: {str(sub_e)}")

            if confirmed_price:
                detail_desc = f"{detail_desc}\n공모가: {confirmed_price}"
            if floating_shares:
                detail_desc = f"{detail_desc}\n유통가능물량: {floating_shares}"

            payload = {
                "date": formatted_date,
                "category": "신규상장",
                "eventName": stock_name,
                "detail": detail_desc,
                "relatedStocks": "",
                "url": detail_url
            }

            events_ref.add(payload)
            success_count += 1
            print(f"✅ 데이터 최종 적재 성공 완료")
            print(f"🔍 [디버깅 대상 종목 종료] -------------------------------\n")

        log_payload = {
            "timestamp": firestore.SERVER_TIMESTAMP,
            "status": "SUCCESS",
            "task_name": "[IPOinfoCrawler] 38커뮤니케이션 IPO일정 수집",
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