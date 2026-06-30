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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, height=64) Chrome/120.0.0.0 Safari/537.36"
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
            floating_amount = ""

            try:
                detail_res = session.get(detail_url, headers=headers, verify=False)
                detail_res.encoding = 'euc-kr'

                if detail_res.status_code == 200:
                    detail_soup = BeautifulSoup(detail_res.text, "html.parser")

                    # 1. 확정공모가 완전 추적 루프
                    detail_tables = detail_soup.find_all("table")
                    for d_table in detail_tables:
                        if "확정공모가" in d_table.get_text():
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

                    # 2. 🎯 유통가능물량 진짜 표 저격 엔진 가동
                    for idx, d_table in enumerate(detail_tables):
                        table_rows = d_table.find_all("tr")
                        if len(table_rows) < 3:
                            continue

                        header_chunk_text = "".join([re.sub(r'\s+', '', r.get_text()) for r in table_rows[:4]])

                        if "유통가능물량" in header_chunk_text and "주식수" in header_chunk_text and "지분율" in header_chunk_text:
                            print(f"  👉 [{idx}번 테이블] 3종 키워드 스캔으로 진짜 유통 표 저격 적중")
                            table_summary = re.sub(r'\s+', ' ', d_table.get_text()).strip()
                            print(f"    - [표 본문 스냅샷]: '{table_summary[:120]}...'")

                            for d_row in reversed(table_rows):
                                d_cells = d_row.find_all(["th", "td"])
                                if not d_cells:
                                    continue

                                cells_list = [re.sub(r'\s+', '', cell.get_text()) for cell in d_cells]
                                row_split_text = "|".join(cells_list)

                                is_summary_row = "합계" in cells_list or "총계" in cells_list or any(
                                    item in ["합계", "총계", "총합계"] for item in cells_list)
                                has_percentage = any("%" in item for item in cells_list)

                                # 💡 [재무제표 노이즈 원천 배제 가드 레이어 이식]
                                is_financial_noise = any(
                                    k in row_split_text for k in ["과목", "자본", "매출", "이익", "손실", "자산총계", "부채총계", "거래등"])

                                if is_summary_row and has_percentage and not is_financial_noise:
                                    print(f"    - '진짜 유통 합계' 결산 행 안전 분할 포착: '{row_split_text}'")

                                    valid_items = []
                                    for item in cells_list:
                                        if re.match(r'^[\d,]+$', item) or re.match(r'^[\d.]+\%$', item):
                                            valid_items.append(item)

                                    print(f"    - 구분 정제된 유효 스펙 데이터 배열: {valid_items}")

                                    if len(valid_items) >= 2:
                                        final_percent = valid_items[-1]
                                        final_shares = valid_items[-2]

                                        if final_percent != "100.00%":
                                            floating_shares = f"{final_shares}주({final_percent})"
                                            print(f"    - 🎯 완벽 수집 성공 결과: {floating_shares}")
                                            break

                            if floating_shares:
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

            # 금액 연산 파트
            if confirmed_price and floating_shares:
                try:
                    num_price = int(re.sub(r'[^\d]', '', confirmed_price))
                    num_shares = int(re.sub(r'[^\d]', '', floating_shares.split('주')[0]))
                    calc_amount_billion = round((num_price * num_shares) / 100000000)
                    if calc_amount_billion > 0:
                        color_circle = "🔴" if calc_amount_billion >= 1000 else "🔵"
                        floating_amount = f"약 {calc_amount_billion:,}억 원 {color_circle}"
                except Exception as calc_err:
                    print(f"⚠️ {stock_name} 유통가능액수 수식 연산 오류: {str(calc_err)}")

            # 순서 구조 확정 연동
            if confirmed_price:
                detail_desc = f"{detail_desc}\n공모가: {confirmed_price}"
            if floating_shares:
                detail_desc = f"{detail_desc}\n유통가능물량: {floating_shares}"
            if floating_amount:
                detail_desc = f"{detail_desc}\n유통가능액수: {floating_amount}"

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