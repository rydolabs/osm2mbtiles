# 벡터 지도 유지·갱신 시간 비교: PostGIS vs Planetiler

**목적:** 최초 빌드 한 번의 속도 대신, 같은 **변경 전·후 OSM 원천 상태**를
두 번 지도에 반영하는 누적 소요시간을 측정한다. 증분 업데이트의 정확성과
출력 안정성이 속도보다 우선이다.

| 단계 | osm2mbtiles/PostGIS | Planetiler 0.10.2 |
|---|---|---|
| 최초 | osm2pgsql --slim import + PostGIS 파생 테이블 + 전체 MVT 출력 | 최초 PBF -> 전체 MBTiles |
| 다음 지도 상태 | 동일 DB에 .osc.gz --append + 파생 SQL + 만료 타일만 갱신 | 갱신된 PBF -> **다시 전체 MBTiles** |
| 누적 | 초기화/초기 생성 + append/flush | **Planetiler 2회 전체 생성** |

Planetiler용 갱신 PBF는 *정확히 같은 OSC 변경분*을
`osmium apply-changes`로 원본 PBF에 적용해 생성한다. 공정하고 보수적으로
주요 수치에서 **PBF 패치 시간은 제외**하고, 별도의 보조 지표로 보여준다.
PostGIS 초기 시간에는 import와 SQL/타일 생성이 **포함**된다.

**조건:** 실제 지역 Geofabrik PBF(해시 기록) + 재현 가능한 인공 hospital
POI 한 개를 추가하는 OSC. 실제 분당 OSM replication의 양·분포를
재현한 시험이 아님. 빈 PostGIS 서버와 실행 환경이 준비된 뒤 측정하며
패키지 설치/다운로드/PostgreSQL 클러스터 생성을 제외한다.

## 자동 실행

GitHub Actions:
`.github/workflows/lifecycle-update-benchmark.yml`

- 브랜치 push: Monaco z12–16 **스모크 테스트**. 작업량이 너무 작아서
  일반적 성능 우위를 증명하지 않는다.
- 수동 `workflow_dispatch`, `region=berlin`: Geofabrik Berlin 전체 PBF,
  범위 `13.20,52.45,13.60,52.60`, z12–16. 공개 수치 전 동일 조건에서
  **서로 독립적인 3회 이상** 재실행하고 중앙값/범위를 보고한다.
- 프로필: `benchmarks/lifecycle-planetiler.yml`은 기존 Tokyo 비교용
  Planetiler YAML을 복사해 입력명만 바꿨다. Planetiler 두 실행 사이
  레이어 프로필은 같으나, **PostGIS SQL 레이어/속성과 동일하지 않다**.

## 계산·검증

```text
Pg_total(2 states) = Pg_initial_import_and_full_export + Pg_append_and_flush
Pl_total(2 states) = Pl_build_initial_PBF + Pl_rebuild_updated_PBF
Pl_total_with_patch = Pl_total + osmium_apply_changes_time   # 보조 수치
```

비교 대상은 **같은 두 시점의 OSM 데이터**이지, 동일한 인코딩 MVT 산출물은
아니다. 결과마다 비어 있지 않은 타일 수와 피처 수를 함께 기록해야 한다.

안전성 검증:
1. 같은 한 개의 인공 변경분이 PostGIS와 Planetiler 출력 양쪽에 반영되어,
   실제로 gzip MVT 타일 데이터가 달라지는지 검사한다.
2. PostGIS 증분 갱신 후의 최종 MBTiles를, 갱신된 PBF를 새로운 PostGIS
   DB에 독립적으로 import해 **전체 재생성한 MBTiles**와 비교한다.
   좌표·레이어·피처·속성·지오메트리·중복 개수의 디코딩 동등성이 필수다.
   이 검증용 재import 시간은 성능 측정에서 제외한다.
3. 독립 DB 사이에서 MVT 내부 피처 순서로 gzip 바이트가 다를 수 있어,
   이 비교에서 전체 MVT 바이트 동일성을 주장하지 않는다. 코드 변경에
   대해서는 **동일 DB·동일 입력의 구버전 대비 바이트 동일성**을 별도
   회귀 조건으로 유지한다.
4. 한 단계라도 실패하면 성능 수치를 확정해 게시하지 않는다.

동일 규모의 변경분이 반복된다는 **가정**하에 추정:
`Pg(N)=Pg_initial+N*Pg_update`,
`Pl(N)=Pl_initial+N*Pl_rebuild`.
이로부터 추정한 손익분기 갱신 횟수는 예측모델이지 실측된 운영 지표가 아니다.

## 결과 표현 원칙

실제로 측정해서 유리한 경우에만 다음 문구를 사용한다.

> 동일한 [지역·고정 PBF·변경분]에서 osm2mbtiles는
> 초기 생성 + 증분 갱신까지 X초,
> Planetiler는 초기 + 갱신 상태 전체 재생성까지 Y초를 기록했다.
> 두 엔진의 MVT 레이어 프로필은 동일하지 않다.

Monaco 한 번의 결과나 단일 인공 POI 갱신만으로 전국 단위 일반화를 하지
않는다. **"Planetiler 전체가 증분 처리를 전혀 지원하지 않는다"**고도
주장하지 않는다. 비교 대상은 명시적으로 Planetiler의
`generate-custom` **두 번 전체 재생성 시나리오**다.

실제 제품의 우선순위는 최초 속도가 아니라 PostGIS 데이터 유지,
벡터 증분 변경분 정확성, OSM Carto PNG/벡터 MBTiles 통합 경험이다.
PNG 증분 갱신과 자동 replication feed는 현재 테스트 대상이 아니다.
