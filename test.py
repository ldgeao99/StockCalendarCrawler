import os
import sys
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# 1. 파이어베이스 인증 키 설정
FIREBASE_KEY_PATH = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"

if not os.path.exists(FIREBASE_KEY_PATH):
    print(f"❌ 에러: 파이어베이스 인증 파일({FIREBASE_KEY_PATH})이 경로에 존재하지 않습니다.")
    sys.exit(1)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = FIREBASE_KEY_PATH

# 2. 클라이언트 및 대상 컬렉션 참조 초기화
db = firestore.Client(project="stockcalender-13042")
logs_ref = db.collection("crawler_logs")

# 삭제 타겟 지정
TARGET_TASK_NAME = "[IPO캘린더] 38커뮤니케이션 수집"



def delete_target_logs():
    print("\n" + "=" * 60)
    print(f"🧹 Firestore 데이터 정리 작업을 시작합니다.")
    print(f"🎯 대상 컬렉션: crawler_logs")
    print(f"🔍 삭제 기준 (task_name): '{TARGET_TASK_NAME}'")
    print("=" * 60)

    try:
        # 3. 조건에 맞는 문서 쿼리 (필터 적용)
        query_ref = logs_ref.where(
            filter=FieldFilter("task_name", "==", TARGET_TASK_NAME)
        )

        docs = query_ref.get()
        target_count = len(docs)

        if target_count == 0:
            print("💡 조건에 부합하는 로그 문서가 존재하지 않습니다. 정리할 작업이 없습니다.")
            print("=" * 60 + "\n")
            return

        print(f"📦 삭제 대상 문서를 발견했습니다. (총 {target_count}건)")
        print("정리 작업을 진행하는 중입니다...\n")

        deleted_count = 0
        # 4. 문서 순회하며 안전하게 일괄 삭제 수행
        for doc in docs:
            # 문서ID 추적용 로그 출력
            print(f"  🗑️  삭제 중: Document ID [{doc.id}]")
            doc.reference.delete()
            deleted_count += 1

        print("\n" + "=" * 60)
        print(f"✨ 정리 작업이 모두 완료되었습니다!")
        print(f"📊 최종 삭제 완료: {deleted_count}건 / {target_count}건")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\n❌ 데이터 삭제 중 치명적인 에러가 발생했습니다: {str(e)}\n")


if __name__ == "__main__":
    delete_target_logs()