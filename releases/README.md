# 버전별 실행파일

테스트할 버전의 Windows 폴더에서 `BTR.exe`를 받거나 저장소를 내려받은 뒤 해당 파일을 실행하세요. Python 설치는 필요하지 않습니다.

| 버전 | 운영체제 | 폴더와 사용 안내 | 실행파일 |
| --- | --- | --- | --- |
| v1.4.1 | Windows 64비트 | [v1.4.1/windows-x64](v1.4.1/windows-x64/README.md) | [BTR.exe 다운로드](https://github.com/xoghlda/BTR/raw/refs/heads/main/releases/v1.4.1/windows-x64/BTR.exe) |
| v1.4.0 | Windows 64비트 | [v1.4.0/windows-x64](v1.4.0/windows-x64/README.md) | [BTR.exe 다운로드](https://github.com/xoghlda/BTR/raw/refs/heads/main/releases/v1.4.0/windows-x64/BTR.exe) |
| v1.3.1 | Windows 64비트 | [v1.3.1/windows-x64](v1.3.1/windows-x64/README.md) | [BTR.exe 다운로드](https://github.com/xoghlda/BTR/raw/refs/heads/main/releases/v1.3.1/windows-x64/BTR.exe) |

각 버전 폴더에는 실행파일, 한글 사용 안내, 빠른 테스트 안내, 개발용 샘플 PDF, 빌드 정보, SHA-256 검증 파일이 함께 있습니다. 배포 파일은 체크섬과 일치하는 원본 바이트를 유지하도록 보관합니다.

ZIP 패키지는 [GitHub Releases](https://github.com/xoghlda/BTR/releases)에서 받을 수 있습니다. 전체 사용법은 [프로젝트 README](../README.md)를 참고하세요.

v1.4.0은 일반 브라우저 창을 직접 선택하고 버튼 위치를 등록해 순서대로 왼쪽 클릭하는 매크로를 추가합니다. 로그인은 사용자가 먼저 완료해야 하며, 매크로가 조회·영수증 발급의 성공을 확인하지는 않습니다. IE 모드 강제 전환이나 코레일 접근 차단 해제 기능은 아닙니다. 등록·재생·긴급 중지 방법은 프로젝트 README의 매크로 안내를 확인하세요.

v1.4.1은 등록·실행 카운트다운 전에 선택 창을 자동 최대화하거나 기록한 내용 영역 크기로 맞춥니다. 새 매크로는 자동 최대화가 기본이며 기존 v1 JSON은 기록 크기 모드로 불러옵니다. 프로필 v2는 창 모드를 보관합니다. 처음 한 번 버튼 좌표를 등록해야 하며, DPI·작업영역이 맞지 않으면 좌표를 스케일하지 않고 중지합니다. Windows 전체 해상도나 브라우저 확대율·스크롤은 변경하지 않습니다.
