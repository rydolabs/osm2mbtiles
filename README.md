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

사전 요구사항: **Windows 10/11 + WSL2 Ubuntu/Debian**, PowerShell, 인터넷 및 충분한 디스크·메모리. Docker 불필요.

```bat
REM 더블클릭 또는 터미널: 대한민국 PBF 자동 다운로드, 기본 PNG
run.bat

REM 대한민국 Vector PBF 또는 PNG+PBF 생성
run.bat vector 12-14
run.bat both 12-14

REM 서울 원본을 별도 다운로드하여 PNG+PBF 테스트
test-seoul.bat
test-seoul.bat 12-15 vector

REM WSL 캐시/작업 DB 관리
cleanup.bat
```

`test-seoul.bat`의 기본 설정은 z12–14, `both`이며 `https://tiles.osm.kr/download/seoul-non-military.osm.pbf`를 사용합니다. 서울 추출 파일의 HeaderBBox가 없을 수 있으므로 기본 `OSM2MBTILES_BBOX=126.75,37.40,127.20,37.72`를 전달합니다. 기존에 받은 PBF는 `test-output/`에 보관하면 재사용합니다.

`run.bat` 기본 입력은 서울 추출이 아닌 **한국 전체 Geofabrik PBF**입니다. 입력/출력/작업 DB는 Windows 및 WSL 파일시스템에서 각각 관리하며 결과 파일은 검증 후 Windows로 복사됩니다. 초기 import에는 시간이 오래 걸릴 수 있습니다.

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

## 성능 — 검증된 범위만 비교

실제 서울 PBF 테스트의 결과입니다. 수치만으로 엔진 전체의 우열을 주장하지 않습니다.

| 측정 대상 | 방식 | 중앙값 | 측정 범위 |
|---|---|---:|---|
| 서울 일부 지역 z12–15, 58타일 | PostGIS 기존 방식 | 12.526초 | **DB import 제외** |
| 동일 58타일 | PostGIS Batch | **1.903초** | **DB import 제외** |
| 동일 58타일 | Planetiler 커스텀 프로필 | 13.137초 | **PBF부터 MBTiles까지** |
| 서울시청 z14/13970/6344, 1타일 | Tilemaker | **5.910초** | PBF부터 MBTiles까지 |
| 동일 1타일 | Planetiler | **10.355초** | PBF부터 MBTiles까지 |
| 동일 1타일 | PostGIS | **1.775초** | **DB import 제외** |

58타일 실험의 일회성 PostGIS import는 별도 12.510초, 1타일 실험은 별도 12.600초였습니다. **PostGIS 타일 전용 시간과 Planetiler/Tilemaker 전체 실행 시간을 직접 비교하면 불공정**합니다. 스타일/출력 피처 개수도 다릅니다. 자세한 측정 방법, 리소스 및 비교의 한계는 [docs/BENCHMARKS.md](docs/BENCHMARKS.md)를 참고하세요.

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
