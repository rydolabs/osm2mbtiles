# osm2mbtiles

**OpenStreetMap PBF → PNG Raster MBTiles + MVT/PBF Vector MBTiles**  
Docker 없이 로컬 PostgreSQL/PostGIS, Mapnik(OSM Carto), osm2pgsql을 이용해 MBTiles를 생성하는 Python 프로젝트입니다. Windows에서는 **WSL2 Ubuntu/Debian**을 사용합니다.

> **상태:** 동작하는 소스 기반 프로토타입입니다. Windows 네이티브 실행 파일이나 모든 의존성이 포함된 포터블 배포판은 제공하지 않습니다. 첫 실행에는 외부 패키지·지도 스타일·폰트·데이터 다운로드가 필요하며, 전국 규모의 장시간 생성과 Windows+WSL 완전 E2E는 추가 검증이 필요합니다.

## 지원하는 출력

| 모드 | 생성 형식 | 설명 |
|---|---|---|
| `--tile-type raster` (기본) | PNG / PNG256 MBTiles | OSM Carto 스타일 + Mapnik으로 실제 그림 타일 렌더링 |
| `--tile-type vector` | gzip MVT(PBF) MBTiles | PostGIS `ST_AsMVT`; 지도 색상·폰트 없이 지오메트리·속성 저장 |
| `--tile-type both` | 두 개의 MBTiles | `<name>.raster.mbtiles` + `<name>.vector.mbtiles` |

**주의:** 입력 `*.osm.pbf`는 OSM 원본이고, 출력 Vector `*.mbtiles` 내부의 PBF는 MVT 타일입니다. 서로 다른 형식입니다. Vector 지도 화면은 별도 MapLibre/Mapbox 스타일이 필요합니다.

## Windows 빠른 시작

사전 요구사항: **Windows 10/11, WSL2 Ubuntu/Debian, PowerShell**, 인터넷과 충분한 디스크 공간.

```bat
REM 국가를 지정하지 않는 기본 실행: 기존 OSM PBF 파일 선택
run.bat

REM 직접 선택한 OSM PBF로 Vector 또는 PNG+Vector 생성
run.bat vector 12-14
run.bat both 12-14

REM 한국 전체 데이터 자동 다운로드는 명시적으로 요청한 경우에만
run.bat south-korea

REM 서울 전용 수동 E2E 테스트 (메인 실행기가 아님)
tests\test-seoul.bat
tests\test-seoul.bat 12-15 vector
```

`tests/test-seoul.bat`는 기존 서울 테스트 전용이며 `test-output/`의 PBF를 재사용합니다. 이 프로젝트의 기본 사용자는 한국 지역에 한정되지 않습니다. 국제 벤치마크 권장 지역은 **베를린(독일)**, 빠른 테스트는 **모나코**입니다. [영어 README](README.md) 및 [국제 성능 벤치마크](docs/BENCHMARKS.md)를 참조하세요.

## Python / Linux · WSL 개발 실행

이 경로는 준비된 PostgreSQL/PostGIS + Mapnik + OSM Carto 런타임이 필요합니다. 소스 코드만 설치해도 네이티브 프로그램이 자동 설치되는 것은 아닙니다.

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest -q

# 준비된 런타임 및 실제 PBF를 사용
python3 -m osm2mbtiles convert seoul.osm.pbf \
  --bbox 126.75,37.40,127.20,37.72 \
  --zoom 12-14 --tile-type both --raster-format png \
  --vector-tiler batch --runtime ./runtime \
  --output seoul.mbtiles --processes 4

python3 -m osm2mbtiles verify seoul.raster.mbtiles
python3 -m osm2mbtiles verify-vector seoul.vector.mbtiles

# 생성 전에 작업량만 확인
python3 -m osm2mbtiles convert seoul.osm.pbf \
  --bbox 126.75,37.40,127.20,37.72 -z 12-14 \
  -o seoul.mbtiles --dry-run
```

Vector 엔진: `--vector-tiler postgis|batch` (기본 postgis), Raster 인코딩: `--raster-format png|png256`. 기존 옵션 `--format` / `--encoding`은 호환 별칭으로 유지합니다. 출력 파일이 존재하면 `--force`가 필요합니다.

## 지도 레이어 범위

도로(`road`), 보행로(`pathway`), 철도(`railway`), 건물(`building`, `building_part`), `poi`, 물(`water`), 지표면(`landcover`), `landuse`, 행정 경계(`boundary`) 등 상세 Vector 레이어를 생성합니다. 이번 이관에서 `natural=rock`을 `landcover`에 포함했으며, `boundary=legal`은 행정 경계와 구별되는 **`boundary_legal`** 레이어(z12+)로 추가했습니다.

실제 서울시청 타일 **z14/x13970/y6344** 재생성 검증에서 `landcover`는 130→**235개**(`natural=rock` 105개 추가), 행정 경계는 **15개 유지**, 새 `boundary_legal`은 **80개**, `water`는 **13개 유지**였습니다. [새 저장소 실제 서울 PBF 검증 결과](https://github.com/rydolabs/osm2mbtiles/actions/runs/40842394686)를 확인하세요.

**레이어 이름과 피처 수는 지도 품질 점수가 아닙니다.** Planetiler/Tilemaker 비교는 서로 다른 분류 YAML/Lua 프로필을 사용했습니다. 실제 `water` / `boundary` 객체가 누락된 것인지 판단하려면 원본 태그와 지오메트리를 비교해야 합니다.

## .osc.gz 증분 업데이트 (Vector만, 실험용)

일반 `convert`의 `--slim --drop` 데이터베이스에는 append할 수 없습니다. **전용 PostGIS DB를 처음부터 새로 생성**하고 osm2pgsql middle 데이터를 보존해야 합니다. 아래 예시는 실행 가능한 별도 PostgreSQL 접속 환경이 준비되었다는 전제입니다.

```bash
export PGHOST=/path/to/socket PGPORT=5432 PGUSER=your_user PGDATABASE=gis

python3 -m osm2mbtiles.vector_incremental init \
  --pbf seoul.osm.pbf --output seoul-incremental.mbtiles \
  --bbox 126.75,37.40,127.20,37.72 --zoom 12-15 --processes 4

python3 -m osm2mbtiles.vector_incremental append \
  --diff change.osc.gz --output seoul-incremental.mbtiles

# 변경 적용은 완료했지만 타일 기록이 실패했을 때만
python3 -m osm2mbtiles.vector_incremental flush \
  --output seoul-incremental.mbtiles
```

생성·수정·삭제 변경의 만료 타일을 계산하고 MBTiles에 UPSERT/DELETE합니다. 실제 `.osc.gz`를 사용한 새로 생성한 MBTiles와의 정합성 검증이 [CI에서 통과](https://github.com/nssang00/chatgpt-write-test/actions/runs/40828739306)했습니다. **PNG 증분, OSM replication sequence 자동 다운로드/검증, 전국 규모 무중단 갱신은 지원하지 않습니다.** 중간 import 실패 후에는 동일 변경 파일을 무조건 재시도하면 안 됩니다. 변경된 Vector SQL을 사용하는 새 버전으로 업그레이드하면 증분 DB 전체 재생성이 필요할 수 있습니다.

## 성능 비교 — osm2mbtiles / Planetiler / Tilemaker

동일한 실제 서울 OSM PBF와 동일한 **z/x/y**를 사용한 기존 Vector MVT 벤치마크 결과입니다. 세 엔진의 **레이어 프로필과 피처 수가 다르므로 작업량이 완전히 동일한 비교는 아닙니다.**

| 엔진 | 서울시청 z14 타일 1개 (2회 중앙값) | 측정 범위 |
|---|---:|---|
| osm2mbtiles PostGIS | **1.775초** | **PostGIS import 완료 후** 타일 생성만 |
| Planetiler v0.10.2 | 10.355초 | PBF 읽기부터 Vector MBTiles 저장까지 |
| Tilemaker v2.4 | 5.910초 | PBF 읽기부터 Vector MBTiles 저장까지 |

> **주의:** PostGIS의 1.775초와 나머지 두 엔진의 시간을 전체 실행 성능으로 직접 비교하면 안 됩니다. 기존 1타일 실험에서 PostGIS 최초 import + 후처리는 별도 12.600초였고, DB 초기화는 포함되지 않았습니다. 이 표는 z14/13970/6344 한 타일의 실제 측정치이지 전국·서울 전체 성능 순위가 아닙니다.

**더 의미 있는 공식 벤치마크로 권장하는 범위는 서울 비교 권역 `126.75,37.40,127.20,37.72`, z12–16 (사각형 범위의 XYZ 격자 8,418개)**입니다. 세 엔진 모두 같은 PBF 스냅샷, BBOX, 줌, 쓰레드 수로 **PBF→Vector MBTiles 전체 시간**을 측정하고, osm2mbtiles는 PostGIS import와 타일 생성 시간을 별도로 기록하는 방식을 권장합니다. 1차 확인용 도심 BBOX `126.95,37.51,127.05,37.60`는 z12–16에서 561개 격자입니다.

**이 범위의 3엔진 결과는 아직 측정 전입니다.** 자세한 58타일 실험, 비교 시 레이어 차이와 재현성 조건은 [벤치마크 방법 및 기존 결과](docs/BENCHMARKS.md)를 참고하세요. 이 비교는 Vector **PBF만** 대상으로 하며 Mapnik **PNG 렌더링 성능은 별도의 벤치마크**가 필요합니다.

## 개발·검증

- `python3 -m pytest -q`: Python 로직, MBTiles 구조, 실행기 계약 테스트
- [원본 실제 Mapnik PNG smoke 테스트](https://github.com/nssang00/chatgpt-write-test/actions/runs/40814498119): Mapnik 3.1.0에서 9개 PNG 타일 생성
- [서울 Vector 동일 타일 비교](https://github.com/nssang00/chatgpt-write-test/actions/runs/40819700167)
- [water / landcover / boundary 분류 진단](https://github.com/nssang00/chatgpt-write-test/actions/runs/40829971952)

CI가 통과해도 모든 Windows WSL 배포판에서 완전 E2E 생성이나 웹 OSM 기본 스타일과의 픽셀 일치를 보장하지는 않습니다.

## 구조

```text
src/osm2mbtiles/   Python CLI, Mapnik PNG worker, PostGIS MVT, incremental updater
vector/             osm2pgsql flex Lua, import 후 SQL
tools/              Windows PowerShell / WSL 스크립트 및 캐시 관리
tests/              회귀 테스트
benchmarks/         재현용 비교 스크립트와 실험 프로필
docs/               속도 비교 조건 및 제한사항
.github/workflows/  자동화 테스트
```

## 라이선스 · 출처

프로젝트 자체 코드는 [MIT](LICENSE). OSM 원본은 OpenStreetMap contributors의 ODbL 및 Attribution 의무를 따릅니다. PostgreSQL/PostGIS, osm2pgsql, Mapnik, OSM Carto, 외부 지도 리소스/폰트는 각각의 라이선스를 따르며 이 저장소에서 해당 바이너리와 지도 데이터 전체를 재배포하지 않습니다.
