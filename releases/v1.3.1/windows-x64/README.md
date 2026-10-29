# BTR v1.3.1 · Windows 64비트

GitHub Releases에 게시한 v1.3.1과 동일한 실행파일입니다. 자차·기차 보고서와 PDF·출력 창 수집 기능을 제공합니다. 브라우저 매크로 기능을 추가한 새 버전은 아닙니다.

**[BTR.exe 바로 다운로드](https://github.com/xoghlda/BTR/raw/refs/heads/main/releases/v1.3.1/windows-x64/BTR.exe)** · [ZIP 패키지](https://github.com/xoghlda/BTR/releases/download/v1.3.1/BTR-v1.3.1-windows-x64.zip)

## 실행과 테스트

1. `BTR.exe`를 다운로드해 실행합니다. Python 설치는 필요하지 않습니다.
2. [사용방법.txt](사용방법.txt)에서 자차·기차별 수집 순서를 확인하세요.
3. 계정 없이 먼저 시험하려면 [빠른테스트.txt](빠른테스트.txt)를 참고하세요. 이 폴더 전체 또는 ZIP을 내려받으면 `samples/DEVELOPMENT_ONLY_SYNTHETIC_3PAGES.pdf` 예제를 사용할 수 있습니다.
4. 기차 모드에서 PDF를 가져오고 3페이지 저장을 확인한 뒤 **코레일 수집 완료**를 눌러 보고서를 확인합니다.

샘플은 개발용 가상자료이며 실제 지출 증빙이 아닙니다. 실제 코레일 로그인·추가 인증은 평소 브라우저에서 직접 진행합니다. 이 테스트 빌드에는 코드 서명이 없습니다.

## 파일 목록

| 파일 | 설명 |
| --- | --- |
| `BTR.exe` | v1.3.1 단일 실행파일 |
| `사용방법.txt` | 자차·기차·PDF·출력 창 사용 안내 |
| `빠른테스트.txt` | 로그인 없이 샘플 PDF로 시험하는 순서 |
| `samples/DEVELOPMENT_ONLY_SYNTHETIC_3PAGES.pdf` | 명확히 표시된 개발용 3페이지 샘플 |
| `BUILDINFO.json` | 빌드 버전·앱 소스 커밋·검증 정보 |
| `SHA256SUMS-in-package.txt` | 위 5개 파일의 SHA-256 |

실행파일 SHA-256:

```text
6120848af49d5ad7f1dc5d54427a15c8c0162e4a5f8c3e016a58b93f2e3bca12
```

PowerShell에서 `Get-FileHash .\BTR.exe -Algorithm SHA256`으로 확인할 수 있습니다. [버전 목록](../../README.md) · [상세 사용법](../../../README.md)
