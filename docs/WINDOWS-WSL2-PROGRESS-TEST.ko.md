# Windows + WSL2 실기 진행률 검증

이 도구는 실제 Windows 컴퓨터의 **Ubuntu/Debian WSL2**에서 프로젝트의 기존 Mapnik/PostGIS 실행 경로를 사용합니다. **GitHub Actions의 Windows runner는 실제 WSL2 검증을 대체할 수 없습니다.**

## 원클릭 실행

1. [진행률 개선 브랜치 전체 ZIP](https://github.com/rydolabs/osm2mbtiles/archive/refs/heads/feat/live-render-progress-20261103.zip)을 받습니다.
2. Windows 로컬 폴더에 **전체 압축을 해제**합니다. BAT 파일만 따로 옮기면 실행되지 않습니다.
3. PowerShell/명령 프롬프트에서 `wsl --list --verbose`를 실행해 기본 배포판이 **Ubuntu 또는 Debian, VERSION 2**인지 확인합니다.
4. 압축 해제한 폴더의 **`TEST-Progress-WSL2.bat`를 더블클릭**합니다. 사전에 PBF 파일을 준비할 필요는 없습니다.
5. `progress-test-results\qa_*.zip`을 찾습니다. ZIP 안의 `summary.json`에서 `mode=real_windows_wsl2`와 `verdict=PASS`를 확인합니다.
6. ZIP 내용에 민감한 정보가 없는지 확인한 후 **그 ZIP만** 대화에 첨부하면 진단할 수 있습니다.

## 실제 검증 내용

- Geofabrik **Monaco 최신 PBF**를 HTTPS로 내려받고 SHA-256을 기록합니다.
- 실제 **`tools/run_windows.ps1` → `tools/run_wsl.sh`**로 `both` 모드를 실행합니다. 영역은 Monaco `7.41,43.72,7.44,43.75`, 줌은 **12–16**, 워커는 2개입니다.
- 실제 `[STAGE] 1/6` ~ `6/6` 출력, Raster와 Vector의 **중간 진행률, 완료 타일 수의 단조 증가, 완료 시 100%, tile/s, ETA**를 검사합니다.
- **Mapnik PNG**와 **gzip MVT Vector MBTiles**의 SQLite 무결성, 메타데이터, 타일 좌표, 비어 있지 않은 데이터, Raster 색상 다양성을 검사합니다.
- 하나의 실제 Raster 타일을 **`preview-mapnik.png`**로 첨부합니다.
- 소요 시간은 Windows 실행기의 실제 `[TIME]` 출력으로 확인합니다.

첫 설치 시에는 Ubuntu의 `sudo` 비밀번호를 물어볼 수 있으며, PostgreSQL/PostGIS·osm2pgsql·Mapnik·OSM Carto, 폰트와 외부 GIS 자산을 설치/다운로드할 수 있습니다. 인터넷과 WSL의 여유 디스크 공간이 필요합니다. 시스템 설치 변경은 **WSL 내부**에서 이루어지며, Windows 프로그램 설치를 수행하지는 않습니다.

**정확한 진행률의 의미:** 퍼센트는 실제로 완료된 **타일 처리량**입니다. PBF 가져오기와 외부 GIS 다운로드처럼 전체 개수를 알 수 없는 단계에서는 진행률을 추측하지 않고 단계·경과 시간과 15초 간격 동작 알림을 표시합니다. 가져오기가 짧게 끝나면 알림이 없어도 정상입니다.

## 결과 ZIP 구성과 개인정보

`progress-test-results`에 다음 구조로 저장됩니다.

```text
progress-test-results/
  qa_YYYYMMDD_HHMMSS_NNNN.zip          <- 공유할 진단 결과
  qa_YYYYMMDD_HHMMSS_NNNN/            <- PC에만 보관
    local-data/                        <- 실제 Monaco PBF, Raster/Vector MBTiles
    raw-logs/                          <- 원본 로그, 공유 금지
    zip-evidence/                      <- ZIP 구성 파일
```

ZIP에는 `summary.json`, `README-RESULTS.txt`, `progress-proof.json`(검증 수행 시), `preview-mapnik.png`(성공 시), `evidence-sha256.txt`, `logs/`(경로·토큰 가림 처리)가 들어갑니다.

**원본 PBF, 전체 MBTiles, DB, 원본 로그는 ZIP에서 제외**합니다. 자동 업로드도 없습니다. 경로와 토큰 가림은 최선의 노력이며 완전한 비식별화를 보장하지 않으므로 **공유 전 ZIP을 직접 확인**하세요.

동시에 원래 사용 중인 WSL 타일 저장소를 건드리지 않도록 `OSM2MBTILES_TEST_RUN_ID`로 작업 캐시를 `~/.cache/osm2mbtiles-release-tests/<테스트ID>`에 분리합니다. 기존 `~/.cache/osm2mbtiles`는 보존합니다. 테스트 폴더와 WSL의 분리된 캐시에는 내려받은 자산이나 로그가 남을 수 있습니다.

## 통과·실패 판정

- **진짜 통과:** ZIP의 `summary.json`에서 `mode`가 `real_windows_wsl2`, `verdict`가 `PASS`이고, `progress-proof.json`의 `passed`가 `true`입니다.
- **실패:** 어떤 단계에서 실패하더라도 가능한 한 결과 ZIP을 생성합니다. ZIP이 있다고 성공한 것은 아닙니다. ZIP 생성 자체가 실패하면 콘솔에 `ZIP ERROR`가 표시됩니다.
- **CI 자체 테스트:** `-ArchiveSelfTest` / `-ArchiveSelfTestFail`는 실제 WSL을 실행하지 않습니다. 결과는 `SELF_TEST_ONLY` 또는 `SIMULATED_FAIL`로 표시하며 배포 성공 증거가 아닙니다.

이번 테스트는 **Raster와 기본 PostGIS Vector 생성**의 진행 상황에 집중합니다. Vector Batch는 별도의 실험 기능이므로 이 실기 도구의 기본 통과 조건에 포함하지 않았습니다. Windows+WSL2에서 이 실기 결과가 나오기 전까지 성공이라고 주장하지 않습니다.

[진행률 표시 설명](PROGRESS-OUTPUT.md) · [PR #34](https://github.com/rydolabs/osm2mbtiles/pull/34)

지도 데이터 © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL 및 출처 표기 준수.
