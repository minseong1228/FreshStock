# Firebase Hosting 배포 준비

현재 구성은 Firebase Hosting(React) → Cloud Run(FastAPI) → Cloud SQL(PostgreSQL)입니다. `/api/**` 요청은 같은 Firebase 주소에서 Cloud Run으로 전달됩니다. Cloud Run 서비스 이름은 `bentostock-api`, 지역은 `asia-northeast3`으로 설정했습니다. 다른 이름이나 지역을 사용하면 `firebase.json`도 같이 바꿉니다.

**공개 전 확인:** 현재 앱은 로그인이나 관리자 권한 구분이 없습니다. 공개 주소를 아는 사람은 메뉴·레시피·재고를 변경하고 주문·취소할 수 있습니다. 실제 개인정보나 운영 데이터를 넣지 마세요. 학교 과제용 공개 시연은 모의 데이터로 진행하고, 공개 범위에 맞는 인증/접근 통제를 구현하기 전에는 실제 운영에 사용하지 마세요. Hosting과 Cloud Run은 인터넷에 공개될 수 있고, URL을 숨기는 것은 접근 통제가 아닙니다.

## 1. 준비

- 결제 계정이 연결된 Google Cloud 프로젝트와 동일한 Firebase 프로젝트. Cloud Run, Cloud SQL, Cloud Build, Artifact Registry, Secret Manager, 네트워크 등에서 요금이 발생할 수 있습니다. Cloud SQL은 시연하지 않는 동안에도 요금이 발생할 수 있으므로 요금과 종료·삭제 정책을 확인하세요.
- Google Cloud CLI, Firebase CLI, Node.js/npm, PostgreSQL 클라이언트 `psql`, Cloud SQL Auth Proxy. Google 계정 로그인은 본인이 진행합니다.
- Cloud SQL PostgreSQL 인스턴스와 빈 `bentostock` 데이터베이스, 최소 권한 사용자. 인스턴스 연결 이름은 `프로젝트ID:지역:인스턴스명`입니다. DB에 실제 데이터를 넣지 마세요.
- Cloud Run API, Cloud SQL Admin API, Cloud Build API, Artifact Registry API, Secret Manager API 등 필요한 API를 활성화하고, Cloud Run 서비스 계정에 Cloud SQL Client 및 해당 비밀의 Secret Manager Secret Accessor 권한을 부여합니다. 빌드·배포 주체에도 필요한 권한이 있어야 합니다.

공식 안내: https://firebase.google.com/docs/hosting/cloud-run , https://cloud.google.com/sql/docs/postgres/connect-run , https://cloud.google.com/run/docs/configuring/services/secrets

## 2. 데이터베이스 초기화 (빈 DB 전용)

PowerShell에서 Cloud SQL Auth Proxy를 인증된 상태로 실행하고, 다른 창에서 아래 명령을 실행합니다. 인스턴스 연결 이름과 DB 사용자는 본인 환경에 맞게 바꾸세요. `schema.sql`과 `seed.sql`은 새 DB에만 사용하며 기존 데이터가 있는 DB에는 반복 실행하지 마세요. 비밀번호를 물으면 직접 입력합니다.

```powershell
# 별도 PowerShell 창: 프록시를 실행한 채로 둡니다.
cloud-sql-proxy.exe --port 5433 "PROJECT_ID:asia-northeast3:INSTANCE_NAME"
```

```powershell
# 프로젝트 폴더에서 실행
psql -h 127.0.0.1 -p 5433 -U bento -d bentostock -f db/schema.sql
psql -h 127.0.0.1 -p 5433 -U bento -d bentostock -f db/seed.sql
```

기존 로컬 주문을 옮기는 작업은 이 초기화에 포함되지 않습니다. 별도 백업·복원 절차가 필요합니다. Auth Proxy 접근 권한 및 네트워크 요구사항은 Google 공식 문서를 확인하세요.

## 3. DB 접속 비밀 등록

Secret Manager에 이름 `bentostock-database-url`의 비밀을 만들고 연결 문자열을 비밀 값으로 등록합니다. DB 계정과 비밀번호는 본인이 입력하고, 소스 파일·Git·명령 기록·채팅에 넣지 마세요. Cloud Run에 연결할 Unix 소켓 방식의 값 예시는 다음 형식입니다.

```text
postgresql://DB_USER:URL_ENCODED_PASSWORD@/bentostock?host=/cloudsql/PROJECT_ID:asia-northeast3:INSTANCE_NAME
```

사용자명이나 비밀번호에 `@`, `:`, `/` 등의 문자가 있으면 URI 인코딩이 필요합니다. 인스턴스의 실제 연결 이름, DB 이름과 계정을 사용하고 서비스 계정에 비밀 접근 및 Cloud SQL Client 권한을 부여하세요.

## 4. Cloud Run 배포

아래는 PowerShell에서 프로젝트 루트 기준 예시입니다. 프로젝트 ID와 인스턴스 이름을 바꾸고, 사용자가 공개 접근의 의미와 요금을 확인한 후 실행합니다. `--allow-unauthenticated`는 API를 인터넷에 공개합니다. Firebase Hosting에서 API를 전달받으려면 해당 방식의 Cloud Run 호출 권한 설정이 필요합니다.

```powershell
gcloud auth login
gcloud config set project PROJECT_ID
gcloud run deploy bentostock-api --source backend --region asia-northeast3 --set-cloudsql-instances PROJECT_ID:asia-northeast3:INSTANCE_NAME --set-secrets DATABASE_URL=bentostock-database-url:latest --allow-unauthenticated --max-instances 2
```

`--max-instances`는 확장을 제한할 뿐 비용 상한이 아닙니다. 예산 알림도 실제 비용을 자동 차단하지 않습니다. Cloud Run 서비스 이름·지역과 `firebase.json`을 일치시키세요. 위 명령은 서비스 배포와 공개 권한 설정을 수행합니다.

## 5. Firebase Hosting 배포

Cloud Run 서비스가 준비된 후 프로젝트 루트에서 실행합니다.

```powershell
cd frontend
npm install
npm run build
cd ..
firebase login
firebase deploy --only hosting --project PROJECT_ID
```

`frontend/dist`가 배포됩니다. 빌드 시 `VITE_API_URL`을 따로 설정하지 않으면 `/api/...`를 현재 사이트에 요청하고 Firebase가 Cloud Run에 전달합니다. `VITE_API_URL`에 로컬 주소가 남아 있으면 클라우드 빌드 전에 제거하세요. Cloud Run 주소로 브라우저에서 직접 요청하도록 별도 도메인을 지정하면 CORS 설정을 추가해야 합니다.

배포된 사이트에서 메뉴 조회, 주문과 취소, 재고 복구를 모의 데이터로 확인하세요. `https://사이트주소/api/menus`가 정상 JSON을 반환하는지도 확인합니다. 오류가 나면 Cloud Run 로그와 Cloud SQL 연결·서비스 계정·시크릿 권한을 확인하세요. 비용을 줄이려면 시연 후 리소스를 직접 확인하고 필요 없는 유료 리소스를 정리하세요.

## 로컬 실행

기존 Docker Compose를 그대로 사용할 수 있습니다: `docker compose up --build`. 로컬 개발 서버는 기본적으로 `http://localhost:8000` API에 연결합니다. 처음 시작할 때만 스키마/시드가 자동으로 적용됩니다. `docker compose down -v`는 저장된 DB를 삭제하므로 주의하세요.
