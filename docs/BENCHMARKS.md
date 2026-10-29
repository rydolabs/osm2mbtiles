# 벤치마크 결과와 공정 비교 기준

아래 값은 실제 벤치마크 증거가 있는 **서울 데이터의 제한된 조건**에서 측정했습니다. 국내 전체 규모의 성능 보장이 아닙니다.

## 58개 벡터 타일, z12–15

| 엔진 | 1회차 | 2회차 | 중앙값 | 측정 포함 단계 |
|---|---:|---:|---:|---|
| osm2mbtiles PostGIS projected | 14.652s | 10.400s | 12.526s | 타일 출력만, DB import 제외 |
| osm2mbtiles Batch SQL | 1.880s | 1.925s | **1.903s** | 타일 출력만, DB import 제외 |
| Planetiler 0.10.2 | 15.840s | 10.434s | 13.137s | PBF 읽기 + MBTiles 출력 |

PostGIS 최초 데이터 import + SQL 준비 **12.510s**는 별도입니다.

## 서울시청 타일 하나: z14/x13970/y6344

동일한 `seoul-non-military.osm.pbf` 입력, Ubuntu 24.04 CI 환경, 각 프로그램 2회 실행, **각 MBTiles에 정확히 한 타일만 존재하도록 확인**.

| 엔진 | Run 1 | Run 2 | 중앙값 | 조건 |
|---|---:|---:|---:|---|
| Tilemaker 2.4 | 5.990s | 5.830s | **5.910s** | PBF→MBTiles |
| Planetiler 0.10.2 | 11.530s | 9.180s | 10.355s | PBF→MBTiles |
| osm2mbtiles PostGIS | 2.870s | 0.680s | 1.775s | 데이터 import **제외** |

PostGIS 최초 import + 후처리는 **12.600s** (DB 자체 초기화 제외). `12.600 + 2.870 = 15.470s`는 분리 측정 구간의 단순 합산일 뿐 하나의 동일한 환경 E2E 실행 시간은 아닙니다.

소스·실행 기록: [GitHub Actions 40826487496](https://github.com/nssang00/chatgpt-write-test/actions/runs/40826487496), 분석 코드 `benchmarks/seoul_three_way_tile.py`, `benchmarks/seoul_generation_speed.py`.

### 성능표 해석 주의

1. **서로 다른 작업량:** Planetiler YAML / Tilemaker Lua 프로필과 osm2mbtiles SQL은 레이어 수 및 피처 선정이 다름. 같은 입력 PBF와 z/x/y라도 동일 객체를 동일 방식으로 처리한다고 말할 수 없음.
2. **PostGIS import 제외:** 이미 DB가 준비된 경우의 타일 출력 속도만 `1.775s`이며, PBF 입력부터의 성능은 이보다 느림.
3. **타일 1개:** 시작 비용과 원본 PBF 읽기가 지배적. 적은 반복수이므로 순위나 배수는 전국 규모 결과로 일반화할 수 없음.
4. **PNG:** 이 표는 Vector PBF 전용. PNG Mapnik 렌더링을 포함한 경쟁 엔진과의 속도 비교 수치는 아직 없음.
5. **증분:** `.osc.gz`에서 변경된 일부 타일만 기록하는 방식은 전체 재생성과 비교하려면 같은 변경 유형, DB 보유 비용, 새로 그리는 타일 수를 포함해야 함.
6. **실험용 Planetiler 프로필:** `benchmarks/seoul-planetiler.yml`의 기존 `natural: '%'` 조건은 바위·모래·숲을 `water`에도 출력하므로 의미론적으로 동등하지 않음. 예전 동일 타일 실측 수치는 이 프로필의 결과이며, 레이어 정확도 품질 지표로 활용하지 말 것.

### 검증된 지도 데이터 분류 차이

같은 서울 타일에서 `water` (osm2mbtiles 13 vs Planetiler 265), `landcover` (130 vs 252), `boundary` (15 vs 146)를 추가로 분석. 대부분은 다른 태그 필터 및 relation/way 분할 방식 때문이었고, 실제 개선 대상 `natural=rock`과 법적 경계 별도 표현은 **이번 이관에서 코드에 추가**. 따라서 **이전 실측 피처 수를 업데이트 후 출력 피처 수로 재사용하면 안 됨**. 적용 후 실증 비교는 별도로 필요.

진단 소스 및 실증 결과: [GitHub Actions 40829971952](https://github.com/nssang00/chatgpt-write-test/actions/runs/40829971952).
